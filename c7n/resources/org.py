# Copyright The Cloud Custodian Authors.
# SPDX-License-Identifier: Apache-2.0

from concurrent.futures import as_completed
import logging
import os
import threading

from botocore.exceptions import ClientError

from c7n.credentials import assumed_session
# from c7n.executor import MainThreadExecutor
from c7n.filters import Filter
from c7n.query import QueryResourceManager, TypeInfo
from c7n.resources.aws import AWS
from c7n.tags import universal_augment
from c7n.utils import local_session, type_schema


log = logging.getLogger("custodian.org-accounts")

ORG_ACCOUNT_SESSION_NAME = "CustodianOrgAccount"


class OrgAccess:
    org_session = None

    def parse_access_role(self):
        params = {}
        for q in self.data.get("query", ()):
            params.update(q)
        return params.get("org-access-role")

    def get_org_session(self):
        # so we have to do a three way dance
        # cli role -> (optional org access role) -> target account role
        #
        # in lambda though if we have a member-role we're effectively
        # already in the org root on the event.
        #
        if self.org_session:
            return self.org_session
        org_access_role = self.parse_access_role()
        if org_access_role and not (
            "LAMBDA_TASK_ROOT" in os.environ and self.data.get("mode", {}).get("member-role")
        ):
            self.org_session = assumed_session(
                role_arn=org_access_role,
                session_name=ORG_ACCOUNT_SESSION_NAME,
                region=self.session_factory.region,
                session=local_session(self.session_factory),
            )
        else:
            self.org_session = local_session(self.session_factory)
        return self.org_session


@AWS.resources.register("org-policy")
class OrgPolicy(QueryResourceManager, OrgAccess):
    policy_types = (
        'SERVICE_CONTROL_POLICY',
        'TAG_POLICY',
        'BACKUP_POLICY',
        'AISERVICES_OPT_OUT_POLICY',
    )

    class resource_type(TypeInfo):
        service = "organizations"
        id = "Id"
        name = "Name"
        arn = "Arn"
        arn_type = "policy"
        enum_spec = ("list_policies", "Policies", None)
        global_resource = True
        permissions_augment = ("organizations:ListTagsForResource",)
        universal_augment = object()

    def resources(self, query=None):
        q = self.parse_query()
        if query is not None:
            q.update(query)
        else:
            query = q
        return super().resources(query=query)

    def augment(self, resources):
        return universal_augment(self, resources)

    def parse_query(self, query=None):
        params = {}
        for q in self.data.get('query', ()):
            if isinstance(q, dict) and 'filter' in q:
                params['Filter'] = q['filter']
        if not params:
            params['Filter'] = "SERVICE_CONTROL_POLICY"
        return params


@AWS.resources.register("org-account")
class OrgAccount(QueryResourceManager, OrgAccess):
    class resource_type(TypeInfo):
        service = "organizations"
        id = "Id"
        name = "Name"
        arn = "Arn"
        arn_type = "account"
        enum_spec = ("list_accounts", "Accounts", None)
        global_resource = True
        permissions_augment = ("organizations:ListTagsForResource",)
        universal_augment = object()

    # executor_factory = MainThreadExecutor
    org_session = None

    def augment(self, resources):
        return universal_augment(self, resources)

    def validate(self):
        self.parse_query()
        return super().validate()

    def parse_query(self):
        params = {}
        for q in self.data.get("query", ()):
            params.update(q)
        self.account_config = {
            k: v for k, v in params.items() if k in ("org-access-role", "org-account-role")
        }
        if "org-account-role" not in self.account_config:
            # Default Organizations Role
            self.account_config["org-account-role"] = "OrganizationAccountAccessRole"

            # Default Organizations Role with Control Tower
            if os.environ.get("AWS_CONTROL_TOWER_ORG"):
                self.account_config["org-account-role"] = "AWSControlTowerExecution"


class AccountHierarchy:
    def get_accounts_for_ous(self, client, ous):
        """get a set of accounts for the given ous ids"""
        account_ids = set()
        for o in ous:
            pager = client.get_paginator("list_children")
            for page in pager.paginate(ParentId=o, ChildType="ACCOUNT"):
                account_ids.update(a["Id"] for a in page.get("Children", []))
        return account_ids

    def get_ous_for_roots(self, client, roots):
        """Walk down the tree from the listed ou roots to collect all nested ous."""
        folders = set(roots)

        while roots:
            r = roots.pop(0)
            pager = client.get_paginator("list_children")
            for page in pager.paginate(ParentId=r, ChildType="ORGANIZATIONAL_UNIT"):
                roots.extend([f["Id"] for f in page.get("Children", [])])
                folders.update([f["Id"] for f in page.get("Children", [])])
        return folders


@OrgAccount.filter_registry.register("ou")
class OrganizationUnit(Filter, AccountHierarchy):
    schema = type_schema("ou", units={"type": "array", "items": {"type": "string"}})
    permissions = ("organizations:ListChildren",)

    def process(self, resources, event=None):
        client = local_session(self.manager.session_factory).client("organizations")
        ous = self.get_ous_for_roots(client, self.data["units"])
        account_ids = self.get_accounts_for_ous(client, ous)
        results = []
        for r in resources:
            if r["Id"] not in account_ids:
                continue
            results.append(r)
        return results


class ProcessAccountSet:
    def resolve_regions(self, account, session):
        return self.data.get("regions", ("us-east-1",))

    def process_account_region(self, account, region, session):
        raise NotImplementedError()

    def process_account(self, account, session):
        log.info(
            "%s processing account:%s id:%s",
            self.type,
            account["Name"],
            account["Id"],
        )
        region_results = {}
        for r in self.resolve_regions(account, session):
            try:
                region_results[r] = self.process_account_region(account, r, session)
            except Exception as e:
                log.exception(
                    "%s account region error %s %s %s error: %s",
                    self.type,
                    account["Name"],
                    account["Id"],
                    r,
                    e,
                )
                region_results[r] = False
        return region_results

    def process_account_set(self, resources):
        account_results = {}
        org_session = self.manager.get_org_session()

        with self.manager.executor_factory(max_workers=8) as w:
            futures = {}
            for a in resources:
                try:
                    s = account_session(
                        org_session, a, self.manager.account_config["org-account-role"]
                    )
                except ClientError:
                    log.error(
                        "%s - error role assuming into %s:%s using role:%s",
                        self.type,
                        a["Name"],
                        a["Id"],
                        self.manager.account_config["org-account-role"],
                    )
                    continue
                futures[w.submit(self.process_account, a, s)] = a
            for f in as_completed(futures):
                a = futures[f]
                if f.exception():
                    log.error(
                        "%s - error in account:%s id:%s error:%s",
                        self.type,
                        a["Name"],
                        a["Id"],
                        f.exception(),
                    )
                    continue
                account_results[a["Id"]] = f.result()
        return account_results


@OrgAccount.filter_registry.register("cfn-stack")
class StackFilter(Filter, ProcessAccountSet):
    schema = type_schema(
        "cfn-stack",
        stack_names={"type": "array", "elements": {"type": "string"}},
        present={"type": "boolean"},
        status={
            "type": "array",
            "items": {
                "enum": [
                    "CREATE_IN_PROGRESS",
                    "CREATE_FAILED",
                    "CREATE_COMPLETE",
                    "ROLLBACK_IN_PROGRESS",
                    "ROLLBACK_FAILED",
                    "ROLLBACK_COMPLETE",
                    "DELETE_IN_PROGRESS",
                    "DELETE_FAILED",
                    "DELETE_COMPLETE",
                    "UPDATE_IN_PROGRESS",
                    "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
                    "UPDATE_COMPLETE",
                    "UPDATE_ROLLBACK_IN_PROGRESS",
                    "UPDATE_ROLLBACK_FAILED",
                    "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
                    "UPDATE_ROLLBACK_COMPLETE",
                    "REVIEW_IN_PROGRESS",
                    "IMPORT_IN_PROGRESS",
                    "IMPORT_COMPLETE",
                    "IMPORT_ROLLBACK_IN_PROGRESS",
                    "IMPORT_ROLLBACK_FAILED",
                    "IMPORT_ROLLBACK_COMPLETE",
                ]
            },
        },
        regions={"type": "array", "elements": {"type": "string"}},
    )

    permissions = ('sts:AssumeRole', 'cloudformation:DescribeStacks')
    annotation = "c7n:cfn-stack"

    def process(self, resources, event=None):
        fresources = []
        results = self.process_account_set(resources)
        for r in resources:
            if r["Id"] not in results:
                continue
            if not any(results[r["Id"]].values()):
                continue
            fresults = {rk: rv for rk, rv in results[r["Id"]].items() if rv}
            r[self.annotation] = fresults
            fresources.append(r)
        return fresources

    def process_account_region(self, account, region, session):
        client = session.client("cloudformation", region_name=region)
        present = self.data.get("present", False)
        states = self.data.get("status", ())

        found = True
        for s in self.data.get("stack_names", ()):
            try:
                stacks = client.describe_stacks(StackName=s).get("Stacks", [])
                if states and stacks[0]["StackStatus"] not in states:
                    found = False
            except ClientError:
                found = False
            else:
                if not stacks:
                    found = False
        if present and found:
            return True
        elif not present and not found:
            return True
        return False


ACCOUNT_SESSION = threading.local()


def account_session(org_session, account, role):
    # differs from local session in being account aware
    # note we expect users of these session to explicitly
    # construct clients by region, as the session as
    # the cache is not region aware.
    #
    # TODO: add cache timeouts.
    if role.startswith("arn"):
        role = role.format(org_account_id=account["Id"])
    else:
        role = f"arn:aws:iam::{account['Id']}:role/{role}"

    org_accounts = getattr(ACCOUNT_SESSION, "org_accounts", {})
    if role in org_accounts:
        return org_accounts[role]

    s = assumed_session(
        role_arn=role,
        session_name=ORG_ACCOUNT_SESSION_NAME,
        session=org_session,
        region=org_session.region_name,
    )

    org_accounts[role] = s
    setattr(ACCOUNT_SESSION, "org_accounts", org_accounts)
    return s
from dataclasses import dataclass

import boto3
import logging

log = logging.getLogger("aws_config")


@dataclass
class AwsSettings:
    region: str
    cfn_bucket: str
    account_id: str = None
    profile: str = None

    def client(self, service):
        session = self._session()
        log.info(f"client({service}), account_id={self.account_id}")
        return session.client(service, region_name=self.region)

    def resource(self, service):
        session = self._session()
        log.info(f"resource({service}), account_id={self.account_id}")
        return session.resource(service, region_name=self.region)

    def get_account_id(self):
        """
        Ensure account-id has been retrieved
        """
        self._session()
        return self.account_id

    def _session(self):
        if self.profile:
            session = boto3.Session(profile_name=str(self.profile))
        else:
            session = boto3.Session()

        if not hasattr(self, "_checked_account"):
            sts = session.client("sts")
            account_id = sts.get_caller_identity()["Account"]
            if self.account_id:
                if str(account_id) != str(self.account_id):
                    raise Exception(f"Incorrect AWS Account - expected {self.account_id}, but appear to be using {account_id} ")

            self._checked_account = True
            self.account_id = account_id

        return session

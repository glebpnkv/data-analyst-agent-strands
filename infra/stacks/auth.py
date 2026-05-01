"""Auth stack: Cognito User Pool for SSO via ALB + Cognito.

Self-signup is OFF — admins create users via `aws cognito-idp
admin-create-user`. New users get a temporary password by email,
sign in, and are forced to change it on first login. Email is the
username; no separate username field. No MFA for the dev demo.

The ALB's authenticate-cognito listener action uses three pieces from
this stack: user_pool (the directory), user_pool_client (the OAuth
app registration with the ALB callback URL configured), and
user_pool_domain (where Cognito hosts the login UI).

Removal policy is DESTROY across the board so teardown_dev_stack.sh
can actually wipe the pool. For prod, switch to RETAIN and set
deletion_protection on the user pool.
"""

import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ssm as ssm
from constructs import Construct


class AuthStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stage: str,
        callback_urls: list[str],
        logout_urls: list[str],
        domain_prefix: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"DataAnalystAgent-{stage}",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
                temp_password_validity=cdk.Duration.days(7),
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            mfa=cognito.Mfa.OFF,
            email=cognito.UserPoolEmail.with_cognito(),  # default no-reply sender
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.user_pool_client = self.user_pool.add_client(
            "AlbClient",
            user_pool_client_name=f"DataAnalystAgent-{stage}-Alb",
            generate_secret=True,  # required for ALB authenticate-cognito
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=callback_urls,
                logout_urls=logout_urls,
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO,
            ],
            prevent_user_existence_errors=True,
            access_token_validity=cdk.Duration.hours(1),
            id_token_validity=cdk.Duration.hours(1),
            refresh_token_validity=cdk.Duration.days(30),
        )

        self.user_pool_domain = self.user_pool.add_domain(
            "HostedDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=domain_prefix,
            ),
        )

        # SSM outputs so admin scripts (add user, etc.) can find the pool
        # without parsing CFN outputs.
        ssm_prefix = f"/data-analyst-agent/{stage.lower()}"
        ssm.StringParameter(
            self,
            "UserPoolIdParam",
            parameter_name=f"{ssm_prefix}/cognito/user-pool-id",
            string_value=self.user_pool.user_pool_id,
        )
        ssm.StringParameter(
            self,
            "UserPoolClientIdParam",
            parameter_name=f"{ssm_prefix}/cognito/user-pool-client-id",
            string_value=self.user_pool_client.user_pool_client_id,
        )
        ssm.StringParameter(
            self,
            "UserPoolDomainParam",
            parameter_name=f"{ssm_prefix}/cognito/user-pool-domain",
            string_value=self.user_pool_domain.domain_name,
        )

        cdk.CfnOutput(
            self,
            "UserPoolId",
            value=self.user_pool.user_pool_id,
            description="aws cognito-idp admin-create-user --user-pool-id <this> ...",
        )
        cdk.CfnOutput(
            self,
            "HostedLoginUrl",
            value=f"https://{self.user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com",
            description="Cognito-hosted login UI base URL",
        )

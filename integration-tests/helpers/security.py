import os
from datetime import datetime, timedelta

from behave import fixture
from behave.runner import Context
from environs import Env
from jose import jwt as jose_jwt


def generate_clinician_token(context: Context) -> str:
    if not hasattr(context, "clinician_jwt"):
        hs_issuer: str = os.environ["HS_ISSUER"]
        hs_key: str = os.environ["HS_KEY"]
        proxy_url: str = os.environ["PROXY_URL"]
        scope: str = os.environ["MOCK_GDM_CLINICIAN_SCOPE"]
        context.clinician_jwt = jose_jwt.encode(
            {
                "metadata": {"clinician_id": context.clinician_uuid},
                "iss": hs_issuer,
                "aud": proxy_url + "/",
                "scope": scope,
                "exp": 9_999_999_999,
            },
            key=hs_key,
            algorithm="HS512",
        )
    context.current_jwt = context.clinician_jwt
    return context.clinician_jwt


def generate_superclinician_token(context: Context) -> str:
    if not hasattr(context, "superclinician_jwt"):
        hs_issuer: str = os.environ["HS_ISSUER"]
        hs_key: str = os.environ["HS_KEY"]
        proxy_url: str = os.environ["PROXY_URL"]
        scope: str = os.environ["MOCK_GDM_SUPERCLINICIAN_SCOPE"]
        context.superclinician_jwt = jose_jwt.encode(
            {
                "metadata": {"clinician_id": context.clinician_uuid},
                "iss": hs_issuer,
                "aud": proxy_url + "/",
                "scope": scope,
                "exp": 9_999_999_999,
            },
            key=hs_key,
            algorithm="HS512",
        )
    context.current_jwt = context.superclinician_jwt
    return context.superclinician_jwt


def generate_patient_token(context: Context) -> str:
    if not hasattr(context, "patient_jwt"):
        hs_issuer: str = os.environ["HS_ISSUER"]
        hs_key: str = os.environ["HS_KEY"]
        proxy_url: str = os.environ["PROXY_URL"]
        scope: str = os.environ["MOCK_GDM_PATIENT_SCOPE"]
        context.patient_jwt = jose_jwt.encode(
            {
                "metadata": {"patient_id": context.patient_uuid},
                "iss": hs_issuer,
                "aud": proxy_url + "/",
                "scope": scope,
                "exp": 9_999_999_999,
            },
            key=hs_key,
            algorithm="HS512",
        )
    context.current_jwt = context.patient_jwt
    return context.patient_jwt


@fixture
def get_system_token(context: Context) -> str:
    return generate_system_token(context)


def generate_system_token(context: Context) -> str:
    if not hasattr(context, "system_jwt"):
        context.system_jwt = jose_jwt.encode(
            claims={
                "metadata": {"system_id": "dhos-robot"},
                "iss": "http://localhost/",
                "aud": "http://localhost/",
                "scope": Env().str("SYSTEM_JWT_SCOPE"),
                "exp": datetime.utcnow() + timedelta(seconds=300),
            },
            key=Env().str("HS_KEY"),
            algorithm="HS512",
        )
    context.current_jwt = context.system_jwt
    return context.system_jwt


def generate_login_token(context: Context) -> str:
    """Special system token used just for login."""
    if not hasattr(context, "login_jwt"):
        if not hasattr(context, "patient_jwt"):
            hs_issuer: str = os.environ["HS_ISSUER"]
            hs_key: str = os.environ["HS_KEY"]
            proxy_url: str = os.environ["PROXY_URL"]
            context.login_jwt = jose_jwt.encode(
                {
                    "metadata": {"system_id": "dhos-robot"},
                    "iss": hs_issuer,
                    "aud": proxy_url + "/",
                    "scope": "read:gdm_clinician_auth_all",
                    "exp": 9_999_999_999,
                },
                key=hs_key,
                algorithm="HS512",
            )
    context.current_jwt = context.login_jwt
    return context.login_jwt

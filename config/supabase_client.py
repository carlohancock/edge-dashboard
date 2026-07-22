"""Supabase client initialization for Edge."""

import os

from dotenv import load_dotenv
from supabase import Client, ClientOptions, create_client

load_dotenv()

POSTGREST_TIMEOUT_SECONDS = 120


def get_supabase_client() -> Client:
    """Create and return a Supabase client using env vars from .env."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        raise ValueError(
            "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY. "
            "Copy .env.example to .env and fill in your keys."
        )

    return create_client(
        url,
        key,
        options=ClientOptions(postgrest_client_timeout=POSTGREST_TIMEOUT_SECONDS),
    )

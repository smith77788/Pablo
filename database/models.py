"""Supabase client for BASIC.FOOD business data."""
import os
from supabase import create_client, Client

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        _client = create_client(url, key)
    return _client


def reset_client() -> None:
    """Force re-initialisation (e.g. after env change in tests)."""
    global _client
    _client = None

class JobPaused(Exception):
    """Cooperative pause signal; not a failure, and it does not trigger a retry."""

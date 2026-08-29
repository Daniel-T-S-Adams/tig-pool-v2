"""Classify slave error strings for quarantine vs retry."""

INFRASTRUCTURE_ERROR_PATTERNS = [
    "cannot open shared object file",
    "no such file or directory",
    "algorithm library",
    "downloading algorithm",
    "challenge container",
    "container not found",
    "permission denied",
    "docker",
    "mount",
]


def is_container_missing_error(error: str) -> bool:
    """Reboot/compose race: challenge container is not in docker ps yet.

    The slave error text also contains 'docker-compose', so the generic
    'docker' infrastructure pattern must not win here. Release the batch;
    do not quarantine a box that is still coming up.
    """
    text = (error or "").lower()
    return "challenge container" in text or "container not found" in text


def is_infrastructure_error(error: str) -> bool:
    if is_container_missing_error(error):
        return False
    text = (error or "").lower()
    return any(pattern in text for pattern in INFRASTRUCTURE_ERROR_PATTERNS)

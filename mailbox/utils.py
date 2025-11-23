from urllib.parse import urlparse

def get_canonical_mailbox_address_identifier(address_url: str) -> str:
    """
    Returns the canonical identifier for a mailbox address.
    This is typically the 'user@host/path' part of the URL, without the protocol.
    """
    parsed = urlparse(address_url)
    # Reconstruct the address without the scheme
    # urlparse('mem:user@host/path') -> scheme='mem', path='user@host/path', netloc='' (for opaque URLs)
    # But 'mem://user@host/path' -> scheme='mem', netloc='user@host', path='/path'

    # The Rust implementation uses Url::parse which handles both.
    # The TS implementation uses `url.protocol` and slices it off?
    # TS: `getCanonicalMailboxAddressIdentifier` implementation isn't fully visible in the snippets,
    # but let's assume it strips the protocol.

    # If the URL is opaque (e.g. mem:user@domain), parsed.path will contain the rest.
    # If it's hierarchical (e.g. mem://user@domain), we need netloc + path.

    if parsed.netloc:
        return f"{parsed.netloc}{parsed.path}"
    return parsed.path

class MailboxError(Exception):
    """Base exception for Mailbox errors."""
    pass

class ProviderNotFound(MailboxError):
    """Raised when a provider for a specific protocol is not found."""
    def __init__(self, protocol: str):
        super().__init__(f"No provider for protocol: {protocol}")
        self.protocol = protocol

class InvalidAddress(MailboxError):
    """Raised when a mailbox address is invalid."""
    def __init__(self, address: str):
        super().__init__(f"Invalid mailbox address: {address}")
        self.address = address

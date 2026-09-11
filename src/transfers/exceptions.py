class TransferError(Exception):
    pass


class IdempotencyConflict(TransferError):
    pass


class SourceWalletAccessDenied(TransferError):
    pass


class WalletNotFound(TransferError):
    pass
# barcodes.py

def build_barcode(isbn: str | None, title: str, library_code: str, sequence: int) -> str:
    """Build '<PREFIX>-<LIBRARY_CODE>-<NNNN>'.

    PREFIX is the ISBN if available, else a slug of the title, else 'BOOK'.
    Sequence is zero-padded to 4 digits; expands naturally past 9999.
    """
    prefix = isbn or _slug(title) or 'BOOK'
    return f'{prefix}-{library_code}-{str(sequence).zfill(4)}'


def _slug(title: str) -> str:
    return ''.join(c for c in (title or '').upper() if c.isalnum())[:8]
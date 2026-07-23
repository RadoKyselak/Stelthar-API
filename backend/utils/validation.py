import re
from typing import Optional

class ValidationError(Exception):
    pass

class InputValidator:

    # Neutralized, not rejected: claims are never rendered as raw HTML (the
    # extension renders via textContent/DOM APIs, not innerHTML), so this is
    # defense-in-depth rather than primary XSS protection. Stripping costs no
    # legitimate claim anything; rejecting produced false positives (see below).
    XSS_PATTERNS = [
        re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL),
        re.compile(r"javascript:", re.IGNORECASE),
        re.compile(r"on\w+\s*=", re.IGNORECASE),
        re.compile(r"<iframe", re.IGNORECASE),
    ]

    CONTROL_CHARS_PATTERN = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]')

    @staticmethod
    def sanitize_claim(claim: str) -> str:
        if not claim:
            raise ValidationError("Claim cannot be empty")

        claim = claim.strip()

        if len(claim) < 3:
            raise ValidationError("Claim must be at least 3 characters long")

        if len(claim) > 5000:
            raise ValidationError("Claim cannot exceed 5000 characters")

        # NOTE: a SQL-injection keyword filter (SELECT/CREATE/DROP/DELETE/...)
        # used to live here and rejected claims outright. This codebase has no
        # SQL or database layer anywhere, so it defended against a threat that
        # doesn't exist — while rejecting real government-research claims like
        # "Congress voted to CREATE a new agency" or "the Senate SELECT
        # committee released its report". Removed entirely rather than tuned,
        # since there is no legitimate use for it here.
        for pattern in InputValidator.XSS_PATTERNS:
            claim = pattern.sub(' ', claim)

        claim = InputValidator.CONTROL_CHARS_PATTERN.sub('', claim)
        claim = re.sub(r'\s+', ' ', claim).strip()

        if len(claim) < 3:
            raise ValidationError("Claim must be at least 3 characters long after removing invalid content")

        return claim
    
    @staticmethod
    def validate_claim(claim: str) -> tuple[bool, Optional[str]]:
        try:
            InputValidator.sanitize_claim(claim)
            return True, None
        except ValidationError as e:
            return False, str(e)
    
    @staticmethod
    def sanitize_api_parameter(param: str, max_length: int = 500) -> str:
        if not param:
            return ""
        
        param = str(param).strip()
        
        if len(param) > max_length:
            param = param[:max_length]
        
        param = InputValidator.CONTROL_CHARS_PATTERN.sub('', param)
        
        return param

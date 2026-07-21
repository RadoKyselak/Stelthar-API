from typing import Literal, Optional, Union
from typing_extensions import TypedDict

SourceType = Literal[
    "BEA", "BLS", "CENSUS", "CONGRESS", "DATA_GOV", "SEARCH_GOV",
    "TREASURY", "USASPENDING", "internal",
]
ErrorStatus = Literal["failed", "missing_data"]

class APIErrorResponse(TypedDict):
    """Standard error response from any API adapter."""
    error: str
    source: SourceType
    status: ErrorStatus

class BaseSourceData(TypedDict, total=False):
    """Base fields common to all successful API responses."""
    title: str
    url: str
    snippet: str
    data_value: Optional[float]
    raw_data_value: Optional[str]

class BEASourceData(BaseSourceData):
    """BEA-specific response data."""
    unit: str
    unit_multiplier: Optional[str]
    line_description: str
    line_code: str

class CensusSourceData(BaseSourceData):
    """Census-specific response data."""
    raw_census_row: dict

class BLSSourceData(BaseSourceData):
    """BLS-specific response data."""
    unit: str

class CongressSourceData(BaseSourceData):
    """Congress.gov-specific response data."""
    pass

class DataGovSourceData(BaseSourceData):
    """Data.gov-specific response data."""
    pass

class TreasurySourceData(BaseSourceData):
    """Treasury Fiscal Data-specific response data."""
    unit: str
    unit_multiplier: int
    record_date: str

class USASpendingSourceData(BaseSourceData):
    """USAspending.gov-specific response data."""
    unit: str
    unit_multiplier: int
    line_description: str

class SearchGovSourceData(BaseSourceData):
    """search.gov research harness response data."""
    content_excerpt: Optional[str]

SourceData = Union[
    BEASourceData,
    CensusSourceData,
    BLSSourceData,
    CongressSourceData,
    DataGovSourceData,
    TreasurySourceData,
    USASpendingSourceData,
    SearchGovSourceData,
    BaseSourceData
]

# API result can be either data or error
APIResult = Union[SourceData, APIErrorResponse]

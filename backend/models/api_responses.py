from typing import TypedDict, Literal, Optional, Union

SourceType = Literal["BEA", "BLS", "CENSUS", "CONGRESS", "DATA.GOV", "LIVE_WEB", "internal"]
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

SourceData = Union[
    BEASourceData,
    CensusSourceData,
    BLSSourceData,
    CongressSourceData,
    DataGovSourceData,
    BaseSourceData
]

# API result can be either data or error
APIResult = Union[SourceData, APIErrorResponse]

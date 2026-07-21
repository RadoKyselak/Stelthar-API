import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
class TestQueryBea:
    """Tests for query_bea function."""
    
    async def test_successful_query(self, sample_bea_response):
        """Test successful BEA API query."""
        import api.bea as mod        
        params = {
            "DataSetName": "NIPA",
            "TableName": "T31600",
            "Frequency": "A",
            "Year": "2023",
            "LineCode": "2"
        }
        
        with patch("api.bea.BEA_API_KEY", "test_bea_key"):
            with patch("api.bea.httpx.AsyncClient") as mock_client_class:
                mock_client = MagicMock()
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = sample_bea_response
                mock_response.url = "https://apps.bea.gov/api/data?..."
                mock_response.raise_for_status = MagicMock()
                
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock()
                
                mock_client_class.return_value = mock_client
                
                result = await mod.query_bea(params)
                
                assert len(result) == 1
                assert result[0]["title"] == "BEA: NIPA/T31600"
                assert result[0]["data_value"] == 790895.0
                assert result[0]["line_code"] == "2"
    
    async def test_missing_api_key(self):
        """Test query_bea with missing API key."""
        import api.bea as mod        
        with patch("api.bea.BEA_API_KEY", None):
            result = await mod.query_bea({"LineCode": "2"})
            assert result[0]["error"] == "BEA_API_KEY missing"
    
    async def test_missing_line_code(self):
        """Test query_bea without LineCode."""
        import api.bea as mod        
        with patch("api.bea.BEA_API_KEY", "test_key"):
            result = await mod.query_bea({})
            assert "error" in result[0]
            assert "missing LineCode" in result[0]["error"]


@pytest.mark.asyncio
class TestQueryCensusAcs:
    """Tests for query_census_acs function."""
    
    async def test_successful_query(self, sample_census_response):
        """Test successful Census ACS query."""
        import api.census as mod        
        params = {
            "year": "2021",
            "dataset": "acs/acs1/profile",
            "get": "NAME,DP05_0001E",
            "for": "state:01"
        }
        
        with patch("api.census.CENSUS_API_KEY", "test_census_key"):
            with patch("api.census.httpx.AsyncClient") as mock_client_class:
                mock_client = MagicMock()
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = sample_census_response
                mock_response.url = "https://api.census.gov/data/..."
                mock_response.headers = {"content-type": "application/json"}
                mock_response.raise_for_status = MagicMock()
                
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock()
                
                mock_client_class.return_value = mock_client
                
                result = await mod.query_census_acs(params)
                
                assert len(result) == 1
                assert "Alabama" in result[0]["title"]
                assert result[0]["data_value"] == 5024279.0
    
    async def test_missing_required_params(self):
        """Test query_census_acs with missing required parameters."""
        import api.census as mod        
        with patch("api.census.CENSUS_API_KEY", "test_key"):
            result = await mod.query_census_acs({"year": "2021"})
            assert result == []


@pytest.mark.asyncio
class TestQueryBls:
    """Tests for query_bls function."""
    
    async def test_successful_cpi_query(self, sample_bls_response):
        """Test successful BLS CPI query."""
        import api.bls as mod        
        params = {"metric": "CPI", "year": "2023"}
        
        with patch("api.bls.BLS_API_KEY", "test_bls_key"):
            with patch("api.bls.httpx.AsyncClient") as mock_client_class:
                mock_client = MagicMock()
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = sample_bls_response
                mock_response.raise_for_status = MagicMock()
                
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock()
                
                mock_client_class.return_value = mock_client
                
                result = await mod.query_bls(params)
                
                assert len(result) == 1
                assert "CPI" in result[0]["title"]
                assert result[0]["unit"] == "%"
                assert result[0]["data_value"] == pytest.approx(4.1, abs=0.1)
    
    async def test_missing_metric(self):
        """Test query_bls with missing metric."""
        import api.bls as mod        
        with patch("api.bls.BLS_API_KEY", "test_key"):
            result = await mod.query_bls({"year": "2023"})
            assert "error" in result[0]
            assert "missing metric" in result[0]["error"]
    
    async def test_unsupported_metric(self):
        """Test query_bls with unsupported metric."""
        import api.bls as mod        
        with patch("api.bls.BLS_API_KEY", "test_key"):
            result = await mod.query_bls({"metric": "INVALID", "year": "2023"})
            assert "error" in result[0]
            assert "not supported" in result[0]["error"]


@pytest.mark.asyncio
class TestQueryCongress:
    """Tests for query_congress function."""
    
    async def test_successful_query(self):
        """Test successful Congress API query."""
        import api.congress as mod        
        mock_response_data = {
            "bills": [
                {
                    "title": "CHIPS Act",
                    "type": "HR",
                    "number": "1234",
                    "congress": "117",
                    "latestAction": {"text": "Passed Senate"}
                }
            ]
        }
        
        with patch("api.congress.CONGRESS_API_KEY", "test_congress_key"):
            with patch("api.congress.httpx.AsyncClient") as mock_client_class:
                mock_client = MagicMock()
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = mock_response_data
                mock_response.raise_for_status = MagicMock()
                
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock()
                
                mock_client_class.return_value = mock_client
                
                result = await mod.query_congress("CHIPS Act")
                
                assert len(result) == 1
                assert "CHIPS Act" in result[0]["title"]
                assert "Passed Senate" in result[0]["snippet"]
    
    async def test_empty_query(self):
        """Test query_congress with empty query."""
        import api.congress as mod        
        with patch("api.congress.CONGRESS_API_KEY", "test_key"):
            result = await mod.query_congress("")
            assert result == []


@pytest.mark.asyncio
class TestQueryDatagov:
    """Tests for query_datagov function."""
    
    async def test_successful_query(self):
        """Test successful Data.gov API query."""
        import api.datagov as mod        
        mock_response_data = {
            "result": {
                "results": [
                    {
                        "title": "Federal Budget Dataset",
                        "name": "federal-budget",
                        "notes": "Dataset description",
                        "resources": [
                            {"url": "https://example.com/data.csv", "format": "csv"}
                        ]
                    }
                ]
            }
        }
        
        with patch("api.datagov.httpx.AsyncClient") as mock_client_class:
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_response_data
            mock_response.raise_for_status = MagicMock()
            
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            
            mock_client_class.return_value = mock_client
            
            result = await mod.query_datagov("federal budget")
            
            assert len(result) == 1
            assert "Federal Budget Dataset" in result[0]["title"]
            assert result[0]["url"] == "https://example.com/data.csv"
    
    async def test_empty_query(self):
        """Test query_datagov with empty query."""
        import api.datagov as mod        
        result = await mod.query_datagov("")
        assert result == []

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

class TestHealthCheckEndpoint:
    """Tests for the health check endpoint."""
    
    def test_health_check(self, test_client):
        """Test GET / returns healthy status."""
        response = test_client.get("/")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "Stelthar-API" in data["message"]

class TestVerifyEndpoint:
    """Tests for the /verify endpoint."""
    
    def test_verify_empty_claim(self, test_client):
        """Empty claims are rejected by request validation.

        VerifyRequest declares ``claim: str = Field(..., min_length=3)``, so an
        empty claim fails schema validation and returns 422 before reaching the
        handler. (The handler's own empty-string check is therefore unreachable.)
        """
        response = test_client.post("/verify", json={"claim": ""})

        assert response.status_code == 422

    def test_verify_too_short_claim(self, test_client):
        """Claims below the 3-character minimum are rejected."""
        response = test_client.post("/verify", json={"claim": "ab"})

        assert response.status_code == 422
    
    def test_verify_missing_claim(self, test_client):
        """Test /verify without claim field returns 422."""
        response = test_client.post("/verify", json={})
        
        assert response.status_code == 422
    
    def test_verify_successful(self, test_client):
        """Test successful /verify request.

        The pipeline now lives in VerificationService (plan -> retrieve ->
        critique -> synthesize), so this patches the service's collaborators
        rather than module-level functions on ``main``.
        """
        analysis = {
            "claim_normalized": "Test claim normalized",
            "claim_type": "quantitative_value",
            "entities": ["Test"],
            "relationship": "equals",
            "api_plan": {
                "tier1_params": {"bea": None, "census": None, "bls": None,
                                 "usaspending": None, "treasury": None},
                "tier2_keywords": ["test"],
            },
        }
        sources = [{
            "title": "Test Source",
            "url": "https://example.com",
            "snippet": "Test data",
            "data_value": 100,
            "year": "2023",
        }]
        synthesis = {
            "verdict": "Supported",
            "summary": "The claim is supported by data.",
            "justification": "Test data shows value of 100.",
            "evidence_links": [{"finding": "Value = 100", "source_url": "https://example.com"}],
        }
        critique = {
            "sufficient": True, "relevance_score": 0.9, "gap": "",
            "relevant_urls": ["https://example.com"], "followup_plan": {},
            "gate": {}, "stopped_reason": None,
        }
        confidence = {"confidence": 0.85, "R": 0.9, "E": 0.8, "S": 0.85, "S_semantic_sim": 0.75}

        with patch("services.verification_service.analyze_claim_for_api_plan",
                   AsyncMock(return_value=analysis)), \
             patch("services.verification_service.execute_query_plan",
                   AsyncMock(return_value=sources)), \
             patch("services.verification_service.critique_evidence",
                   AsyncMock(return_value=critique)), \
             patch("services.verification_service.synthesize_finding_with_llm",
                   AsyncMock(return_value=synthesis)), \
             patch("confidence.confidence_scorer.ConfidenceScorer.compute_confidence",
                   AsyncMock(return_value=confidence)):
            response = test_client.post("/verify", json={"claim": "Test claim"})

        assert response.status_code == 200
        data = response.json()

        assert data["claim_original"] == "Test claim"
        assert data["verdict"] == "Supported"
        assert data["confidence"] == 0.85
        # The loop's audit trail should be present on every response.
        assert len(data["debug_iterations"]) == 1

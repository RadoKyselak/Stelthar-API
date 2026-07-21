import pytest
from confidence import (
    ConfidenceScorer,
    ReliabilityScorer,
    EvidenceDensityScorer,
    SemanticAlignmentScorer
)
from models.api_responses import SourceData
from config.constants import CONFIDENCE_CONFIG


class TestReliabilityScorer:
    """Tests for ReliabilityScorer."""
    
    def test_empty_sources(self):
        scorer = ReliabilityScorer()
        result = scorer.score([])
        assert result == 0.5
    
    def test_bea_source(self):
        scorer = ReliabilityScorer()
        sources = [{"url": "https://apps.bea.gov/api/data", "title": "BEA Data"}]
        result = scorer.score(sources)
        assert result == 1.0
    
    def test_mixed_sources(self):
        scorer = ReliabilityScorer()
        sources = [
            {"url": "https://apps.bea.gov/api/data"},
            {"url": "https://catalog.data.gov/dataset"},
        ]
        result = scorer.score(sources)
        assert result == pytest.approx(0.85, abs=0.01)


class TestEvidenceDensityScorer:
    def test_empty_sources(self):
        scorer = EvidenceDensityScorer()
        result = scorer.score([])
        assert result == 0.0
    
    def test_one_source(self):
        scorer = EvidenceDensityScorer()
        sources = [{"url": "test"}]
        result = scorer.score(sources)
        assert result == round(1 / CONFIDENCE_CONFIG.MAX_SOURCES_FOR_FULL_DENSITY, 2)
    
    def test_five_sources(self):
        scorer = EvidenceDensityScorer()
        sources = [{"url": f"test{i}"} for i in range(5)]
        result = scorer.score(sources)
        assert result == 1.0
    
    def test_capped_at_one(self):
        scorer = EvidenceDensityScorer()
        sources = [{"url": f"test{i}"} for i in range(10)]
        result = scorer.score(sources)
        assert result == 1.0


@pytest.mark.asyncio
class TestSemanticAlignmentScorer:
    """Tests for SemanticAlignmentScorer."""
    
    async def test_verdict_confidence_supported(self):
        scorer = SemanticAlignmentScorer()
        s_llm = scorer._get_verdict_confidence("Supported")
        assert s_llm == CONFIDENCE_CONFIG.VERDICT_CONFIDENCE_SUPPORTED
    
    async def test_verdict_confidence_contradicted(self):
        scorer = SemanticAlignmentScorer()
        s_llm = scorer._get_verdict_confidence("Contradicted")
        assert s_llm == CONFIDENCE_CONFIG.VERDICT_CONFIDENCE_CONTRADICTED
    
    async def test_verdict_confidence_inconclusive(self):
        scorer = SemanticAlignmentScorer()
        s_llm = scorer._get_verdict_confidence("Inconclusive")
        assert s_llm == CONFIDENCE_CONFIG.VERDICT_CONFIDENCE_INCONCLUSIVE
        # An unsupported verdict must not read as a confident one.
        assert s_llm < CONFIDENCE_CONFIG.VERDICT_CONFIDENCE_SUPPORTED


@pytest.mark.asyncio
class TestConfidenceScorer:
    """Integration tests for ConfidenceScorer."""
    
    async def test_no_sources(self):
        scorer = ConfidenceScorer()
        result = await scorer.compute_confidence([], "Inconclusive", "test claim")
        assert result["confidence"] == CONFIDENCE_CONFIG.DEFAULT_CONFIDENCE
        assert result["R"] == CONFIDENCE_CONFIG.DEFAULT_R
        assert result["E"] == CONFIDENCE_CONFIG.DEFAULT_E
    
    def test_confidence_tier_high(self):
        assert ConfidenceScorer.get_confidence_tier(0.8) == "High"
    
    def test_confidence_tier_medium(self):
        assert ConfidenceScorer.get_confidence_tier(0.6) == "Medium"
    
    def test_confidence_tier_low(self):
        assert ConfidenceScorer.get_confidence_tier(0.3) == "Low"

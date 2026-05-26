import asyncio
import json
from typing import Dict, Any, List
from fastapi import HTTPException
from config import logger
from config.constants import CONFIDENCE_CONFIG
from models.verdicts import VerificationResponse
from models.confidence import ConfidenceBreakdown
from confidence.confidence_scorer import ConfidenceScorer
from services import (
    analyze_claim_for_api_plan,
    execute_query_plan,
    synthesize_finding_with_llm,
)


class VerificationService:
    
    def __init__(self):

        self.confidence_scorer = ConfidenceScorer()
    
    async def verify_claim(self, claim: str) -> VerificationResponse:

        start_time = asyncio.get_event_loop().time()

        analysis = {}
        all_results = []
        synthesis_result = {}
        confidence_breakdown: ConfidenceBreakdown = {}

        try:
            analysis = await analyze_claim_for_api_plan(claim)
            claim_norm = analysis.get("claim_normalized", claim)
            claim_type = analysis.get("claim_type", "Other")
            api_plan = analysis.get("api_plan", {})

            logger.info("Generated API Plan: %s", json.dumps(api_plan, indent=2))

            all_results = await execute_query_plan(api_plan, claim_type)

            sources_results = [r for r in all_results if isinstance(r, dict) and "error" not in r]
            debug_errors = [r for r in all_results if isinstance(r, dict) and "error" in r]
            debug_errors = self._dedupe_errors(debug_errors)

            logger.info(
                f"Retrieved {len(sources_results)} sources, encountered {len(debug_errors)} errors."
            )

            synthesis_result = await synthesize_finding_with_llm(claim, analysis, sources_results)
            verdict = synthesis_result.get("verdict", "Inconclusive")
            summary_text = (
                f"{synthesis_result.get('summary','')}. {synthesis_result.get('justification','')}"
                .strip()
                .replace("..", ".")
            )

            confidence_breakdown = await self.confidence_scorer.compute_confidence(
                sources=sources_results,
                verdict=verdict,
                claim=claim_norm
            )
            confidence_val = confidence_breakdown["confidence"]
            
            confidence_tier = self.confidence_scorer.get_confidence_tier(confidence_val)

            end_time = asyncio.get_event_loop().time()
            duration = round(end_time - start_time, 2)
            logger.info(f"Verification completed for claim '{claim[:50]}...' in {duration} seconds.")

            return self._build_success_response(
                claim=claim,
                claim_norm=claim_norm,
                claim_type=claim_type,
                verdict=verdict,
                summary_text=summary_text,
                confidence_val=confidence_val,
                confidence_tier=confidence_tier,
                confidence_breakdown=confidence_breakdown,
                synthesis_result=synthesis_result,
                sources_results=sources_results,
                analysis=analysis,
                debug_errors=debug_errors
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Unexpected error during verification.")
            return self._build_error_response(claim, analysis, all_results, e)
    

    def _dedupe_errors(self, errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        deduped = []
        for err in errors:
            key = (err.get("source"), err.get("status"), err.get("error"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(err)
        return deduped

    def _build_success_response(
        self,
        claim: str,
        claim_norm: str,
        claim_type: str,
        verdict: str,
        summary_text: str,
        confidence_val: float,
        confidence_tier: str,
        confidence_breakdown: ConfidenceBreakdown,
        synthesis_result: Dict[str, Any],
        sources_results: List[Dict[str, Any]],
        analysis: Dict[str, Any],
        debug_errors: List[Dict[str, Any]]
    ) -> VerificationResponse:
        """Build successful verification response."""
        return {
            "claim_original": claim,
            "claim_normalized": claim_norm,
            "claim_type": claim_type,
            "verdict": verdict,
            "confidence": confidence_val,
            "confidence_tier": confidence_tier,
            "confidence_breakdown": {
                "source_reliability": confidence_breakdown.get("R", 0.0),
                "evidence_density": confidence_breakdown.get("E", 0.0),
                "semantic_alignment": confidence_breakdown.get("S", 0.0),
            },
            "summary": summary_text,
            "evidence_links": synthesis_result.get("evidence_links", []),
            "sources": sources_results[:20],
            "debug_plan": analysis,
            "debug_log": debug_errors,
        }
    
    def _build_error_response(
        self,
        claim: str,
        analysis: Dict[str, Any],
        all_results: List[Dict[str, Any]],
        error: Exception
    ) -> VerificationResponse:
        """Build error verification response."""
        return {
            "claim_original": claim,
            "claim_normalized": analysis.get("claim_normalized", claim),
            "claim_type": analysis.get("claim_type", "Other"),
            "verdict": "Error",
            "confidence": 0.0,
            "confidence_tier": "Low",
            "confidence_breakdown": {
                "source_reliability": 0.0,
                "evidence_density": 0.0,
                "semantic_alignment": 0.0,
            },
            "summary": f"An error occurred while processing this claim: {str(error)}",
            "evidence_links": [],
            "sources": [r for r in all_results if r and "error" not in r] if all_results else [],
            "debug_plan": analysis,
            "debug_log": [r for r in all_results if r and "error" in r] + [{
                "error": f"Unhandled exception during processing: {str(error)}",
                "source": "internal_verify_endpoint",
                "status": "failed",
            }],
        }

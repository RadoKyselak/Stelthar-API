"""Agentic verification loop.

Replaces the original single-shot pipeline (plan -> query -> synthesize -> done),
which returned "Inconclusive" the moment the first query plan missed. The loop
now critiques what it retrieved and re-plans against the specific gap, so a bad
first guess no longer ends the verification.

    iteration 1..N:
        execute plan  ->  critique evidence
                          |- sufficient?  -> synthesize
                          |- insufficient -> merge follow-up plan, retry
    (bounded by AGENT_CONFIG.MAX_ITERATIONS and GLOBAL_TIMEOUT_SECONDS)

Every attempt is recorded in `debug_iterations` so the path is auditable.
"""
import asyncio
import json
import re
from typing import Dict, Any, List, Set

from fastapi import HTTPException

from config import logger
from config.constants import AGENT_CONFIG, CONFIDENCE_CONFIG
from models.verdicts import VerificationResponse
from models.confidence import ConfidenceBreakdown
from confidence.confidence_scorer import ConfidenceScorer
from services import (
    analyze_claim_for_api_plan,
    execute_query_plan,
    synthesize_finding_with_llm,
)
from services.critic import critique_evidence, is_data_bearing, is_metadata_only


class VerificationService:

    def __init__(self):
        self.confidence_scorer = ConfidenceScorer()

    async def verify_claim(self, claim: str) -> VerificationResponse:
        start_time = asyncio.get_event_loop().time()

        analysis: Dict[str, Any] = {}
        all_sources: List[Dict[str, Any]] = []
        all_errors: List[Dict[str, Any]] = []
        iterations_log: List[Dict[str, Any]] = []
        critique: Dict[str, Any] = {}

        try:
            analysis = await analyze_claim_for_api_plan(claim)
            claim_norm = analysis.get("claim_normalized", claim)
            claim_type = analysis.get("claim_type", "Other")
            plan = analysis.get("api_plan", {})

            logger.info("Initial API plan: %s", json.dumps(plan, default=str))

            seen_keys: Set[str] = set()

            for iteration in range(1, AGENT_CONFIG.MAX_ITERATIONS + 1):
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > AGENT_CONFIG.GLOBAL_TIMEOUT_SECONDS:
                    logger.warning("Global verification timeout at iteration %d.", iteration)
                    iterations_log.append({
                        "iteration": iteration,
                        "skipped": "global_timeout",
                        "elapsed_s": round(elapsed, 2),
                    })
                    break

                results = await execute_query_plan(plan, claim_type, seen_keys=seen_keys)

                new_sources = [r for r in results if isinstance(r, dict) and "error" not in r]
                new_errors = [r for r in results if isinstance(r, dict) and "error" in r]

                all_sources.extend(new_sources)
                all_errors.extend(new_errors)

                critique = await critique_evidence(
                    claim=claim,
                    analysis=analysis,
                    sources=all_sources,
                    iteration=iteration,
                    max_iterations=AGENT_CONFIG.MAX_ITERATIONS,
                )

                iterations_log.append({
                    "iteration": iteration,
                    "plan": plan,
                    "new_sources": len(new_sources),
                    "new_errors": len(new_errors),
                    "cumulative_sources": len(all_sources),
                    "sufficient": critique.get("sufficient"),
                    "relevance_score": critique.get("relevance_score"),
                    "gap": critique.get("gap"),
                })

                if critique.get("sufficient"):
                    logger.info("Evidence sufficient after iteration %d.", iteration)
                    break

                followup = critique.get("followup_plan") or {}
                if not followup or not self._plan_has_queries(followup):
                    logger.info(
                        "No usable follow-up plan at iteration %d; concluding with what we have.",
                        iteration,
                    )
                    break

                logger.info("Re-planning (iteration %d) to close gap: %s",
                            iteration + 1, str(critique.get("gap"))[:150])
                plan = followup

            # Prefer the sources the critic judged on-topic; fall back to all.
            ranked_sources = self._rank_sources(all_sources, critique)

            synthesis_result = await synthesize_finding_with_llm(claim, analysis, ranked_sources)
            verdict = synthesis_result.get("verdict", "Inconclusive")
            summary_text = self._compose_summary(synthesis_result)

            confidence_breakdown = await self.confidence_scorer.compute_confidence(
                sources=ranked_sources,
                verdict=verdict,
                claim=claim_norm,
            )
            confidence_val = confidence_breakdown["confidence"]

            confidence_val, breakdown_notes = self._apply_confidence_penalties(
                claim=claim,
                claim_norm=claim_norm,
                confidence_val=confidence_val,
                confidence_breakdown=confidence_breakdown,
                sources=ranked_sources,
                critique=critique,
                verdict=verdict,
            )

            confidence_tier = self.confidence_scorer.get_confidence_tier(confidence_val)

            duration = round(asyncio.get_event_loop().time() - start_time, 2)
            logger.info(
                "Verification finished in %.2fs after %d iteration(s): verdict=%s confidence=%.2f",
                duration, len(iterations_log), verdict, confidence_val,
            )

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
                "sources": ranked_sources[:20],
                "debug_plan": analysis,
                "debug_log": self._dedupe_errors(all_errors),
                "debug_iterations": iterations_log,
                "debug_notes": breakdown_notes,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Unexpected error during verification.")
            return self._build_error_response(claim, analysis, all_sources, all_errors, iterations_log, e)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _plan_has_queries(plan: Dict[str, Any]) -> bool:
        """True if a plan would actually issue at least one query."""
        tier1 = plan.get("tier1_params") or {}
        if any(isinstance(v, dict) and v for v in tier1.values()):
            return True
        kws = plan.get("tier2_keywords") or []
        return bool([k for k in kws if isinstance(k, str) and k.strip()])

    @staticmethod
    def _rank_sources(
        sources: List[Dict[str, Any]],
        critique: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Order sources so real datapoints lead and catalog metadata trails.

        Synthesis reads the context top-down, so putting hard numbers first is
        what stops the LLM from anchoring on a Data.gov landing page.
        """
        relevant_urls = set(critique.get("relevant_urls") or [])

        def rank(s: Dict[str, Any]) -> tuple:
            return (
                0 if is_data_bearing(s) else 1,          # numbers first
                0 if s.get("url") in relevant_urls else 1,  # critic-approved next
                1 if is_metadata_only(s) else 0,         # catalog pages last
            )

        # Drop exact duplicate URLs while preserving the best-ranked instance.
        deduped: Dict[str, Dict[str, Any]] = {}
        for s in sorted(sources, key=rank):
            url = s.get("url") or f"_nourl_{id(s)}"
            key = f"{url}::{s.get('line_code', '')}::{s.get('year', '')}"
            if key not in deduped:
                deduped[key] = s
        return sorted(deduped.values(), key=rank)

    @staticmethod
    def _compose_summary(synthesis_result: Dict[str, Any]) -> str:
        summary = (synthesis_result.get("summary") or "").strip()
        justification = (synthesis_result.get("justification") or "").strip()
        if summary and justification:
            joiner = " " if summary.endswith((".", "!", "?")) else ". "
            return f"{summary}{joiner}{justification}"
        return summary or justification or "No summary available."

    def _apply_confidence_penalties(
        self,
        claim: str,
        claim_norm: str,
        confidence_val: float,
        confidence_breakdown: ConfidenceBreakdown,
        sources: List[Dict[str, Any]],
        critique: Dict[str, Any],
        verdict: str,
    ) -> tuple:
        """Cap confidence when the evidence does not actually earn it."""
        notes: List[str] = []

        # 1. No hard datapoint at all -> this is at best a weak signal.
        if not any(is_data_bearing(s) for s in sources):
            confidence_val = min(confidence_val, 0.40)
            notes.append("No numeric datapoint retrieved; confidence capped at 0.40.")

        # 2. Critic judged the evidence only loosely related.
        relevance = critique.get("relevance_score")
        if isinstance(relevance, (int, float)) and relevance < CONFIDENCE_CONFIG.RELEVANCE_SIMILARITY_FLOOR:
            confidence_val = min(confidence_val, 0.50)
            notes.append(f"Critic relevance {relevance:.2f} below floor; capped at 0.50.")

        # 3. Temporal mismatch: claim names a year the evidence does not cover.
        claim_year = self._extract_claim_year(claim_norm) or self._extract_claim_year(claim)
        source_years = self._extract_source_years(sources)
        if claim_year and source_years and claim_year not in source_years:
            confidence_val = min(confidence_val, 0.55)
            confidence_breakdown["E"] = min(confidence_breakdown.get("E", 0.0), 0.5)
            notes.append(
                f"Claim year {claim_year} not covered by source years "
                f"{sorted(source_years)}; capped at 0.55."
            )

        # 4. An Inconclusive verdict should never read as confident.
        if verdict == "Inconclusive":
            confidence_val = min(confidence_val, 0.45)

        return round(max(0.0, min(1.0, confidence_val)), 2), notes

    @staticmethod
    def _extract_claim_year(text: str) -> str | None:
        m = re.search(r"\b(19\d{2}|20\d{2})\b", text or "")
        return m.group(1) if m else None

    @staticmethod
    def _extract_source_years(sources: List[Dict[str, Any]]) -> Set[str]:
        years: Set[str] = set()
        for s in sources:
            for field in ("year", "raw_year", "record_date"):
                val = s.get(field)
                if not val:
                    continue
                m = re.search(r"\b(19\d{2}|20\d{2})\b", str(val))
                if m:
                    years.add(m.group(1))
        return years

    @staticmethod
    def _dedupe_errors(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        deduped = []
        for err in errors:
            key = (err.get("source"), err.get("status"), err.get("error"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(err)
        return deduped

    def _build_error_response(
        self,
        claim: str,
        analysis: Dict[str, Any],
        sources: List[Dict[str, Any]],
        errors: List[Dict[str, Any]],
        iterations_log: List[Dict[str, Any]],
        error: Exception,
    ) -> VerificationResponse:
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
            "sources": sources or [],
            "debug_plan": analysis,
            "debug_log": self._dedupe_errors(errors) + [{
                "error": f"Unhandled exception during processing: {str(error)}",
                "source": "internal_verify_endpoint",
                "status": "failed",
            }],
            "debug_iterations": iterations_log,
            "debug_notes": [],
        }

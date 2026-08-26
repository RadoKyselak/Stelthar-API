from typing import Dict, Any, List

from exceptions import LLMException, MiradorException
from config.constants import LLM_CONFIG
from config import logger
from utils.parsing import extract_json_block
from .llm import call_gemini

async def synthesize_finding_with_llm(
    claim: str,
    claim_analysis: Dict[str, Any],
    sources: List[Dict[str, Any]]
) -> Dict[str, Any]:
    
    default_response = {
        "degraded": False,
        "verdict": "Inconclusive",
        "summary": "Could not determine outcome based on available data.",
        "justification": "No supporting government data was found or the analysis failed.",
        "evidence_links": [],
    }

    valid_sources = [s for s in sources if s and "error" not in s]
    if not valid_sources:
        error_sources = [s for s in sources if s and "error" in s]
        if error_sources:
            default_response["justification"] += f" (API errors encountered: {len(error_sources)})."
        else:
            default_response["justification"] += " (No relevant data sources found)."
        return default_response

    context_parts = []
    source_map_for_linking = {}

    for idx, s in enumerate(valid_sources):
        source_id = f"Source_{idx+1}"
        url = s.get('url', 'N/A') or 'N/A'
        title = s.get('title', 'N/A')
        part = f"<{source_id}>\nSource Title: {title}\nURL: {url}\n"

        data_point_text = None
        snippet = (s.get('snippet') or 'N/A').strip()
        has_value = s.get("data_value") is not None

        if "apps.bea.gov" in url and has_value:
            raw_val = s.get('raw_data_value', 'N/A')
            unit = s.get('unit', '')
            mult = s.get('unit_multiplier')
            line_desc = s.get('line_description', 'BEA Data')
            line_code = s.get('line_code', '')
            data_point_text = f"{line_desc} ({line_code}) = {raw_val}{' '+unit if unit else ''}"
            part += f"Data Point: {data_point_text} (Multiplier: {mult})\n"
        elif "api.census.gov" in url and has_value:
            raw_val = s.get('raw_data_value', 'N/A')
            data_point_text = f"{title} = {raw_val}"
            part += f"Data Point: {data_point_text}\n"
        elif "bls.gov" in url and has_value:
            data_point_text = snippet
            part += f"Data Point: {data_point_text}\n"
        elif has_value:
            # USAspending / Treasury and any future numeric source.
            raw_val = s.get('raw_data_value', 'N/A')
            unit = s.get('unit', '')
            line_desc = s.get('line_description', title)
            data_point_text = f"{line_desc} = {raw_val}{' '+unit if unit else ''}"
            part += f"Data Point: {data_point_text}\n"

        # Mark valueless sources explicitly. Without this the model treats a
        # dataset catalog blurb as if it were evidence for a number.
        if not has_value:
            part += "Data Point: NONE - this source contains no datapoint (descriptive/catalog only)\n"

        if s.get("year"):
            part += f"Data Year: {s['year']}\n"

        part += f"Snippet: {snippet}\n</{source_id}>\n"
        context_parts.append(part)

        if data_point_text:
            source_map_for_linking[data_point_text] = url
        source_map_for_linking[snippet] = url

    context = "\n---\n".join(context_parts)
    MAX_CONTEXT_LENGTH = 30000
    if len(context) > LLM_CONFIG.MAX_CONTEXT_LENGTH:
        context = context[:LLM_CONFIG.MAX_CONTEXT_LENGTH] + "\n... [Context Truncated]"

    prompt = f"""
    You are an objective fact-checker. Analyze the provided evidence from U.S. government sources against the user's claim.

    USER'S CLAIM: '''{claim}'''
    Claim Analysis:
    - Normalized: {claim_analysis.get('claim_normalized', claim)}
    - Type: {claim_analysis.get('claim_type', 'Unknown')}
    - Entities: {claim_analysis.get('entities', [])}
    - Asserted Relationship: {claim_analysis.get('relationship', 'Unknown')}

    AVAILABLE EVIDENCE (Each source is tagged with <Source_N>):
    {context}

    INSTRUCTIONS:
    0.  **CRITICAL - evidence discipline:** A source whose "Data Point" is NONE is
        descriptive metadata (e.g. a dataset catalog entry). It proves only that a
        dataset exists — it is NOT evidence for or against any number. NEVER issue
        "Supported" or "Contradicted" based on such sources alone; if no source
        carries a real datapoint addressing the claim, the verdict is "Inconclusive".
    0b. **Timeframe discipline:** If the claim specifies a year and the only relevant
        datapoints carry a different "Data Year", say so explicitly in the summary and
        prefer "Inconclusive" over asserting a verdict from the wrong period.
    1.  Carefully review the user's claim and its asserted relationship between entities.
    2.  Examine ALL evidence provided within the <Source_N> tags. Focus on data points (BEA, Census, BLS, USAspending, Treasury) directly relevant to the claim's entities and timeframe.
    3.  **BEA Data:** Apply the 'Multiplier' if provided (e.g., a 'DataValue' of 1000 and 'Multiplier' of 1000000 means 1,000,000,000). Assume "Millions of dollars" (multiplier 1,000,000) for BEA NIPA tables like T31600 if multiplier is null/missing but units aren't specified.
    4.  **Census Data:** Use the provided 'Data Point' values directly.
    5.  **BLS Data:** Use the 'Data Point' which represents a calculated percentage (e.g., 3.5 for 3.5% or a raw index value). The Snippet/Title clarifies the metric.
    6.  Compare relevant findings from the evidence to the claim's assertion. Perform calculations if necessary (e.g., comparisons).
    7.  Determine the final `verdict`:
        - "Supported": If evidence *clearly and directly* supports the claim's assertion.
        - "Contradicted": If evidence *clearly and directly* contradicts the claim's assertion.
        - "Inconclusive": If evidence is missing, insufficient, ambiguous, irrelevant, or requires assumptions beyond the data to make a clear judgment.
    8.  Write a concise `summary` (1-2 sentences) stating the final conclusion and the key evidence. **Format large numbers clearly using 'billion' or 'trillion' where appropriate (e.g., '$790.2 billion').**
    9.  Write a brief `justification` (1-2 sentences) explaining *why* you reached that verdict, referencing specific data points or lack thereof.
    10. Create `evidence_links` (list of {{"finding": "...", "source_url": "..."}}) linking **only the most crucial data points** or findings cited in the justification back to their source URLs. Use the specific data identifier and value (e.g., "National defense (G16046) = 790,895") or a concise summary of the finding as the "finding" value. **The 'finding' string MUST NOT include the 'Unit' text (like 'Millions of Dollars') or the 'Multiplier' text (like '(Multiplier: 1000000)').** Match the finding precisely to the source URL provided in the evidence context. Limit to 2-3 key links.

    Return ONLY a single valid JSON object with keys: "verdict", "summary", "justification", "evidence_links".

    Example Response (BEA Comparison):
    {{
      "verdict": "Contradicted",
      "summary": "The data contradicts the claim; federal spending on national defense ($790.9 billion) significantly exceeded spending on education ($178.6 billion) in 2023.",
      "justification": "BEA NIPA T31600 data for 2023 shows Federal National Defense (G16046) spending was $790,895 million, while Federal Education (G16068) spending was $178,621 million.",
      "evidence_links": [
        {{"finding": "National defense (G16046) = 790,895", "source_url": "https://apps.bea.gov/api/data?..."}},
        {{"finding": "Education (G16068) = 178,621", "source_url": "https://apps.bea.gov/api/data?..."}}
      ]
    }}
    """

    try:
        res = await call_gemini(prompt)
        parsed = extract_json_block(res.get("text", ""))

        if parsed and all(k in parsed for k in ["verdict", "summary", "justification", "evidence_links"]):
            if parsed["verdict"] not in ["Supported", "Contradicted", "Inconclusive"]:
                logger.warning("LLM returned invalid verdict: %s. Defaulting to Inconclusive.", parsed["verdict"])
                parsed["verdict"] = "Inconclusive"

            corrected_links = []
            if isinstance(parsed.get("evidence_links"), list):
                for link in parsed["evidence_links"]:
                    if isinstance(link, dict) and "finding" in link and "source_url" in link:
                        # Coerce before matching. A non-string finding (the
                        # model returning a bare number, which the prompt
                        # invites) previously raised TypeError here and was
                        # swallowed as "no supporting data was found".
                        finding = link.get("finding")
                        if not isinstance(finding, str):
                            finding = "" if finding is None else str(finding)
                            link["finding"] = finding

                        best_match_url = source_map_for_linking.get(finding)
                        if not best_match_url and finding:
                            for text, url in source_map_for_linking.items():
                                if not isinstance(text, str):
                                    continue
                                if finding in text or text in finding:
                                    best_match_url = url
                                    break
                        link["source_url"] = best_match_url if best_match_url else link["source_url"]
                        corrected_links.append(link)
                    else:
                        corrected_links.append(link)
                parsed["evidence_links"] = corrected_links

            if not parsed.get("evidence_links"):
                # Prefer sources that actually carry a datapoint over catalog blurbs.
                fallback_pool = [s for s in valid_sources if s.get("data_value") is not None] \
                    or valid_sources
                parsed["evidence_links"] = [
                    {
                        "finding": (s.get("snippet") or s.get("title") or "Evidence")[:120],
                        "source_url": s.get("url", ""),
                    }
                    for s in fallback_pool[:2]
                    if s.get("url")
                ]

            return parsed
        else:
            logger.error("Failed to parse valid synthesis JSON from LLM response: %s", res.get("text", ""))
            default_response["justification"] += " (LLM response parsing failed.)"
            return default_response
            
    except LLMException as e:
        # The analysis service failed. That is NOT a finding about the claim,
        # and must never be rendered as one.
        logger.error("Synthesis LLM call failed: %s", e.message)
        return {
            **default_response,
            "summary": "Verification could not be completed.",
            "justification": (
                f"The analysis service failed ({e.message}). This is not a "
                f"judgment about the claim."
            ),
            "degraded": True,
            "degraded_reason": "llm_unavailable",
        }
    except MiradorException as e:
        logger.exception("Synthesis failed with an application error.")
        return {
            **default_response,
            "summary": "Verification could not be completed.",
            "justification": "The verification pipeline failed before reaching a conclusion.",
            "degraded": True,
            "degraded_reason": e.__class__.__name__,
        }
    except Exception:
        logger.exception("Unexpected error during LLM synthesis.")
        return {
            **default_response,
            "summary": "Verification could not be completed.",
            "justification": "The verification pipeline failed before reaching a conclusion.",
            "degraded": True,
            "degraded_reason": "unexpected_error",
        }

from typing import List, Optional
from config import logger
from config.constants import CONFIDENCE_CONFIG
from models.api_responses import SourceData
from models.verdicts import VerdictType
from utils.similarity import cosine_similarity

# NOTE: services.llm is imported lazily inside _calculate_semantic_similarity.
# Importing it at module scope creates a cycle:
#   confidence/__init__ -> semantic_scorer -> services.llm -> services/__init__
#   -> services.confidence -> confidence.confidence_scorer -> (partially
#   initialised confidence package)
# which made `import confidence` fail depending on import order.


class SemanticAlignmentScorer:
    """Calculates semantic alignment (S) using LLM verdict and cosine similarity."""
    
    def __init__(self):
        self.config = CONFIDENCE_CONFIG
    
    async def score(
        self,
        sources: List[SourceData],
        verdict: VerdictType,
        claim: str
    ) -> tuple[float, float]:
        S_llm_verdict = self._get_verdict_confidence(verdict)
        
        S_semantic_sim = await self._calculate_semantic_similarity(sources, claim)
        
        S_combined = round(
            (S_llm_verdict * self.config.S_LLM_WEIGHT) + 
            (S_semantic_sim * self.config.S_EMBEDDING_WEIGHT),
            2
        )
        
        return S_combined, S_semantic_sim
    
    def _get_verdict_confidence(self, verdict: VerdictType) -> float:
        if verdict == "Supported":
            return self.config.VERDICT_CONFIDENCE_SUPPORTED
        elif verdict == "Contradicted":
            return self.config.VERDICT_CONFIDENCE_CONTRADICTED
        else:
            return self.config.VERDICT_CONFIDENCE_INCONCLUSIVE
    
    async def _calculate_semantic_similarity(
        self,
        sources: List[SourceData],
        claim: str
    ) -> float:
        from services.llm import get_embeddings_batch_api  # lazy: see module note

        try:
            texts_to_embed = self._prepare_texts_for_embedding(claim, sources)

            if len(texts_to_embed) <= 1:
                return self.config.DEFAULT_S_SEMANTIC

            all_embeddings = await get_embeddings_batch_api(texts_to_embed)
            
            if not all_embeddings or all_embeddings[0] is None:
                logger.warning("Claim embedding failed, cannot calculate semantic similarity.")
                return self.config.DEFAULT_S_SEMANTIC

            claim_embedding = all_embeddings[0]
            source_embeddings = [emb for emb in all_embeddings[1:] if emb is not None]
            
            if not source_embeddings:
                logger.warning("No valid source embeddings returned.")
                return self.config.DEFAULT_S_SEMANTIC
            
            similarities = [
                cosine_similarity(claim_embedding, emb)
                for emb in source_embeddings
            ]
            
            if similarities:
                return round(float(max(similarities)), 2)
            else:
                return self.config.DEFAULT_S_SEMANTIC
                
        except ValueError as ve:
            logger.error(f"Cannot compute semantic similarity: {ve}")
            return 0.1
        except Exception as e:
            logger.error(f"Error during semantic similarity calculation: {e}", exc_info=True)
            return self.config.DEFAULT_S_SEMANTIC
    
    def _prepare_texts_for_embedding(
        self,
        claim: str,
        sources: List[SourceData]
    ) -> List[str]:
        texts = []
        
        if claim and claim.strip():
            texts.append(claim.strip())
        else:
            raise ValueError("Claim text is empty, cannot compute semantic similarity.")
        
        for source in sources:
            if source.get('data_value') is not None:
                data_text = f"{source.get('line_description', source.get('title', 'Data'))}: {source.get('raw_data_value')}"
                texts.append(data_text.strip())
            
            snippet = source.get('snippet', '').strip()
            if snippet:
                texts.append(snippet)

            title = source.get('title', '').strip()
            if title and title not in texts:
                texts.append(title)
        
        return texts

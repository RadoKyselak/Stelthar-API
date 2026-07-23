import pytest
from utils.similarity import cosine_similarity

class TestCosineSimilarity:
    """Tests for cosine_similarity function."""
    
    def test_identical_vectors(self):
        """Test similarity of identical vectors (should be 1.0)."""
        vec = [1.0, 2.0, 3.0]
        result = cosine_similarity(vec, vec)
        assert result == pytest.approx(1.0)
    
    def test_orthogonal_vectors(self):
        """Test similarity of orthogonal vectors (should be 0.0)."""
        vec1 = [1.0, 0.0, 0.0]
        vec2 = [0.0, 1.0, 0.0]
        result = cosine_similarity(vec1, vec2)
        assert result == pytest.approx(0.0)
    
    def test_opposite_vectors(self):
        """Test similarity of opposite vectors (should be -1.0)."""
        vec1 = [1.0, 2.0, 3.0]
        vec2 = [-1.0, -2.0, -3.0]
        result = cosine_similarity(vec1, vec2)
        assert result == pytest.approx(-1.0)
    
    def test_similar_vectors(self):
        """Test similarity of similar vectors."""
        vec1 = [1.0, 2.0, 3.0]
        vec2 = [1.0, 2.1, 2.9]
        result = cosine_similarity(vec1, vec2)
        assert 0.9 < result < 1.0
    
    def test_empty_vectors(self):
        """Test with empty vectors."""
        result = cosine_similarity([], [])
        assert result == 0.0
    
    def test_different_lengths(self):
        """Test with vectors of different lengths."""
        vec1 = [1.0, 2.0]
        vec2 = [1.0, 2.0, 3.0]
        result = cosine_similarity(vec1, vec2)
        assert result == 0.0
    
    def test_zero_magnitude_vector(self):
        """Test with zero-magnitude vector."""
        vec1 = [0.0, 0.0, 0.0]
        vec2 = [1.0, 2.0, 3.0]
        result = cosine_similarity(vec1, vec2)
        assert result == 0.0
    
    def test_none_vectors(self):
        """Test with None vectors."""
        result = cosine_similarity(None, [1.0, 2.0])
        assert result == 0.0
        
        result = cosine_similarity([1.0, 2.0], None)
        assert result == 0.0

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestChromaStore:
    @patch("harness.tools.knowledge_retrieval.chromadb.PersistentClient")
    def test_init_creates_collection(self, mock_client: MagicMock) -> None:
        mock_collection = MagicMock()
        mock_client.return_value.get_collection.return_value = mock_collection

        from harness.tools.knowledge_retrieval import KnowledgeRetrievalTool

        tool = KnowledgeRetrievalTool()
        assert tool.collection is mock_collection

    @patch("harness.tools.knowledge_retrieval.chromadb.PersistentClient")
    def test_run_empty_kb(self, mock_client: MagicMock) -> None:
        mock_collection = MagicMock()
        mock_collection.count.return_value = 0
        mock_client.return_value.get_collection.return_value = mock_collection

        import asyncio
        from harness.tools.knowledge_retrieval import KnowledgeRetrievalTool

        tool = KnowledgeRetrievalTool()
        result = asyncio.run(tool.run(query="测试"))
        assert "空" in result

"""Document Q&A tools — answer from the user's own files, with citations."""

from typing import List

from pydantic import BaseModel, Field

from ..documents import DocumentIndex
from ..registry import Tool


class SearchDocsParams(BaseModel):
    query: str = Field(
        ...,
        min_length=2,
        description="What to look for in the user's documents.",
    )


def document_tools(index: DocumentIndex) -> List[Tool]:
    def search_documents(query: str) -> dict:
        found = index.search(query)
        return {
            "query": query,
            "matches": [
                {"citation": m["citation"], "excerpt": m["excerpt"]}
                for m in found.get("matches", [])
            ],
            "note": found.get("note", ""),
        }

    def list_documents() -> dict:
        docs = index.documents()
        return {"documents": docs, "count": len(docs)}

    return [
        Tool(
            name="search_documents",
            description=(
                "Search the user's own documents (contracts, specs, notes, reports) "
                "and return passages WITH citations. Use this whenever the question "
                "is about their files, or refers to 'my document', 'the contract', "
                "'the spec', or anything the model could not otherwise know. "
                "Always cite the file in your answer."
            ),
            parameters=SearchDocsParams,
            handler=search_documents,
        ),
        Tool(
            name="list_documents",
            description="List the documents currently indexed and available to search.",
            parameters=None,
            json_schema={"type": "object", "properties": {}},
            handler=list_documents,
        ),
    ]

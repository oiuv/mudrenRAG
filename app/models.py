
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

class RetrievalSetting(BaseModel):
    top_k: int
    score_threshold: float

class Condition(BaseModel):
    name: List[str]
    comparison_operator: str
    value: Optional[str] = None

class MetadataCondition(BaseModel):
    logical_operator: Optional[str] = 'and'
    conditions: List[Condition]

class RetrievalRequest(BaseModel):
    knowledge_id: str
    query: str
    retrieval_setting: RetrievalSetting
    metadata_condition: Optional[MetadataCondition] = None

class Record(BaseModel):
    content: str
    score: float
    title: str
    metadata: Optional[Dict[str, Any]] = None

class RetrievalResponse(BaseModel):
    records: List[Record]

class ErrorResponse(BaseModel):
    error_code: int
    error_msg: str

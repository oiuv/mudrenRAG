from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr,
    StringConstraints, model_validator,
)

from .config import MAX_TEXT_LENGTH

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ComparisonOperator = Literal[
    "contains", "not contains", "start with", "end with", "is", "is not",
    "in", "not in", "empty", "not empty", "=", "≠", ">", "<", "≥", "≤",
    "before", "after",
]


class RetrievalSetting(BaseModel):
    top_k: int = Field(ge=1, le=100, strict=True)
    score_threshold: float = Field(ge=0, le=1, allow_inf_nan=False)


class Condition(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    name: NonEmptyString
    comparison_operator: ComparisonOperator
    value: StrictStr | StrictInt | StrictFloat | list[StrictStr] | None = None

    @model_validator(mode="after")
    def validate_operand(self):
        operator = self.comparison_operator
        if operator in ("empty", "not empty"):
            return self
        if self.value is None:
            raise ValueError("value is required for this comparison operator.")
        if operator in ("contains", "not contains", "start with", "end with", "before", "after"):
            if not isinstance(self.value, str):
                raise ValueError("This comparison requires a string value.")
        if operator in ("in", "not in") and not isinstance(self.value, list):
            raise ValueError("in/not in requires an array of strings.")
        if operator in ("=", "≠", ">", "<", "≥", "≤") and not isinstance(self.value, (int, float)):
            raise ValueError("Numeric comparison requires a number.")
        if operator in ("is", "is not") and isinstance(self.value, list):
            raise ValueError("is/is not requires a scalar value.")
        if operator in ("before", "after"):
            from .metadata import parse_date
            if parse_date(self.value) is None:
                raise ValueError("Date comparison requires an ISO 8601 date or timestamp.")
        return self


class MetadataCondition(BaseModel):
    logical_operator: Literal["and", "or"] = "and"
    conditions: list[Condition] = Field(max_length=50)


class RetrievalRequest(BaseModel):
    knowledge_id: NonEmptyString
    query: Annotated[NonEmptyString, Field(max_length=MAX_TEXT_LENGTH)]
    retrieval_setting: RetrievalSetting
    metadata_condition: MetadataCondition | None = None


class Record(BaseModel):
    content: str
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    title: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalResponse(BaseModel):
    records: list[Record]


class ErrorResponse(BaseModel):
    error_code: int
    error_msg: str

from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

FIELDS = ('room.length_m','room.width_m','room.height_m','room.openings_m2','room.surface_condition',
 'budget_minor','currency','region','start_date','deadline','hard_preferences','soft_preferences','excluded_work',
 'calendar.timezone','calendar.work_weekdays','calendar.day_start_hour','calendar.hours_per_day','calendar.crew_count')

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class UserInput(StrictModel):
    text: str = Field(min_length=1,max_length=16000)
    fields: dict[str,JsonValue] = Field(default_factory=dict)
    @field_validator('fields')
    @classmethod
    def allowed(cls,v):
        if set(v)-set(FIELDS): raise ValueError('Неизвестное поле формы')
        return v

class FactCandidate(StrictModel):
    field_path: str = Field(json_schema_extra={"enum":list(FIELDS)})
    value: str
    input_id: str
    quote: str = Field(min_length=1)
    @field_validator('field_path')
    @classmethod
    def allowed(cls,v):
        if v not in FIELDS: raise ValueError('Неизвестный путь')
        return v

class Ambiguity(StrictModel):
    kind: Literal['value_conflict','semantic']
    field_path: Literal[*FIELDS] | None
    message: str = Field(min_length=1)

    @model_validator(mode='after')
    def conflict_requires_path(self):
        if self.kind=='value_conflict' and self.field_path is None:
            raise ValueError('Конфликт требует имени поля')
        return self

class Extraction(StrictModel):
    candidates: list[FactCandidate]
    ambiguities: list[Ambiguity]
    unsupported_requests: list[str]

class CommandMeta(StrictModel):
    action_id: str = Field(min_length=1,max_length=100)
    expected_version: int = Field(ge=0)

class Answer(StrictModel):
    question_id: str
    text: str = Field(min_length=1,max_length=4000)

class CreateCommand(StrictModel):
    action_id: str = Field(min_length=1,max_length=100)
    input: UserInput

class AnswersCommand(CommandMeta):
    answers: list[Answer] = Field(min_length=1)

class EditCommand(CommandMeta):
    input: UserInput

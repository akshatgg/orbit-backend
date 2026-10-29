from pydantic import BaseModel, Field, field_validator


class DirectStart(BaseModel):
    user_id: str


class MessageCreate(BaseModel):
    body: str = Field(max_length=4000)

    @field_validator("body")
    @classmethod
    def _length(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 2000:
            raise ValueError("A message is 1 to 2000 characters")
        return value


class BlockCreate(BaseModel):
    user_id: str

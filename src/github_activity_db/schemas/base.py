"""Base schema class with factory pattern for ORM conversion."""

from typing import Any, Self, TypeVar

from pydantic import BaseModel, ConfigDict

# Type variable for SQLAlchemy model classes
ModelT = TypeVar("ModelT")


class SchemaBase(BaseModel):
    """Base class for all Pydantic schemas with ORM conversion support."""

    model_config = ConfigDict(
        from_attributes=True,
        str_strip_whitespace=True,
    )

    @classmethod
    def from_orm(cls, obj: Any) -> Self:
        """
        Factory method to create a schema instance from a SQLAlchemy model.

        Args:
            obj: SQLAlchemy model instance

        Returns:
            Pydantic schema instance
        """
        return cls.model_validate(obj)

    @classmethod
    def from_orm_list(cls, objs: list[Any]) -> list[Self]:
        """
        Factory method to create schema instances from a list of SQLAlchemy models.

        Args:
            objs: List of SQLAlchemy model instances

        Returns:
            List of Pydantic schema instances
        """
        return [cls.from_orm(obj) for obj in objs]

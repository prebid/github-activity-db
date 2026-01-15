"""Tests for UserTag Pydantic schemas."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from github_activity_db.db.models import UserTag
from github_activity_db.schemas import UserTagCreate, UserTagRead


class TestUserTagCreate:
    """Tests for UserTagCreate schema."""

    def test_user_tag_create_valid(self):
        """Test valid tag data is accepted."""
        tag = UserTagCreate(
            name="needs-review",
            description="PRs that need code review",
            color="#ff9900",
        )
        assert tag.name == "needs-review"
        assert tag.description == "PRs that need code review"
        assert tag.color == "#ff9900"

    def test_user_tag_create_minimal(self):
        """Test tag with only required fields."""
        tag = UserTagCreate(name="urgent")
        assert tag.name == "urgent"
        assert tag.description is None
        assert tag.color is None

    def test_user_tag_color_valid_hex(self):
        """Test valid hex color codes are accepted."""
        # Lowercase
        tag = UserTagCreate(name="test", color="#ff0000")
        assert tag.color == "#ff0000"

        # Uppercase should be normalized to lowercase
        tag = UserTagCreate(name="test", color="#FF0000")
        assert tag.color == "#ff0000"

        # Mixed case
        tag = UserTagCreate(name="test", color="#aAbBcC")
        assert tag.color == "#aabbcc"

    def test_user_tag_color_invalid_format(self):
        """Test invalid color formats are rejected."""
        # Named color
        with pytest.raises(ValidationError) as exc_info:
            UserTagCreate(name="test", color="red")
        assert "color" in str(exc_info.value).lower()

        # Short hex (3 chars)
        with pytest.raises(ValidationError):
            UserTagCreate(name="test", color="#fff")

        # Missing hash
        with pytest.raises(ValidationError):
            UserTagCreate(name="test", color="ff0000")

        # Too long
        with pytest.raises(ValidationError):
            UserTagCreate(name="test", color="#ff00000")

        # Invalid characters
        with pytest.raises(ValidationError):
            UserTagCreate(name="test", color="#gggggg")

    def test_user_tag_color_normalized_lowercase(self):
        """Test that color is normalized to lowercase."""
        tag = UserTagCreate(name="test", color="#ABCDEF")
        assert tag.color == "#abcdef"

        tag = UserTagCreate(name="test", color="#AbCdEf")
        assert tag.color == "#abcdef"


class TestUserTagRead:
    """Tests for UserTagRead schema."""

    async def test_user_tag_read_from_orm(self, db_session, sample_user_tag):
        """Test ORM conversion works correctly."""
        tag = UserTag(**sample_user_tag)
        db_session.add(tag)
        await db_session.flush()

        tag_read = UserTagRead.from_orm(tag)

        assert tag_read.id == tag.id
        assert tag_read.name == "needs-review"
        assert tag_read.description == "PRs that need code review"
        assert tag_read.color == "#ff9900"
        assert isinstance(tag_read.created_at, datetime)

    async def test_user_tag_read_with_null_fields(self, db_session):
        """Test reading tag with null optional fields."""
        tag = UserTag(name="minimal")
        db_session.add(tag)
        await db_session.flush()

        tag_read = UserTagRead.from_orm(tag)

        assert tag_read.name == "minimal"
        assert tag_read.description is None
        assert tag_read.color is None

"""The consumer views on seeded data (redesign plan 5.1, D4, D5, D28, D29, D31)."""

from __future__ import annotations

import itertools
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from github_activity_db.db.models import Base
from tests.db.alembic_support import query
from tests.db.schema_support import views
from tests.db.seed import T0, Seed, connect

REPO = "prebid/Prebid.js"
PR_KEYS = [
    "repository_github_id",
    "repository_full_name",
    "repository_is_tracked",
    "pr_number",
    "pr_github_id",
]


SINGLE_COLUMN_INDEXES = (
    "DROP INDEX ix_pr_reviews_pr_id_reviewer_id_github_id",
    "DROP INDEX ix_pr_reviews_reviewer_id",
    "CREATE INDEX ix_pr_reviews_pr_id ON pr_reviews (pr_id)",
    "CREATE INDEX ix_pr_reviews_reviewer_id ON pr_reviews (reviewer_id)",
)


def account(prefix: str) -> list[str]:
    return [
        f"{prefix}_github_id",
        f"{prefix}_node_id",
        f"{prefix}_login",
        f"{prefix}_type",
        f"{prefix}_automation_reason",
    ]


COLUMNS = {
    "v_github_users": [
        "github_id",
        "node_id",
        "login",
        "type",
        "name",
        "created_at",
        "updated_at",
        "linked_user_github_id",
        "claimant_github_id",
        "refreshed_at",
        "not_found_at",
        "automation_reason",
    ],
    "v_account_profiles": [
        "github_id",
        "node_id",
        "login",
        "type",
        "automation_reason",
        "company",
        "location",
        "website_url",
        "description",
        "email",
        "twitter_username",
        "is_verified",
        "archived_at",
        "has_sponsors_listing",
        "pinned_items_remaining",
        "refreshed_at",
    ],
    "v_pr_reviews": [
        *PR_KEYS,
        "review_github_id",
        "review_node_id",
        "state",
        "created_at",
        "submitted_at",
        "published_at",
        "updated_at",
        "last_edited_at",
        "includes_created_edit",
        "commit_oid",
        "author_association",
        "author_can_push_to_repository",
        "inline_comments_total",
        "reviewer_reported",
        *account("reviewer"),
        *account("editor"),
        "is_pr_author",
    ],
    "v_pr_comments": [
        *PR_KEYS,
        "comment_github_id",
        "comment_node_id",
        "created_at",
        "published_at",
        "updated_at",
        "last_edited_at",
        "created_via_email",
        "includes_created_edit",
        "author_association",
        "author_reported",
        *account("author"),
        *account("editor"),
        "is_pr_author",
        "author_also_reviewed",
    ],
    "v_commit_authors": [
        *PR_KEYS,
        "sha",
        "linked_github_id",
        "author_name",
        "author_email",
        "authored_at",
        *account("account"),
        "email_automation_reason",
    ],
    "v_pr_relationships": [
        *PR_KEYS,
        "relationship",
        "review_github_id",
        "comment_github_id",
        "request_node_id",
        "request_github_id",
        "request_ordinal",
        "commit_sha",
        "reported",
        *account("account"),
        "account_not_found_at",
        "review_state",
        "review_submitted_at",
        "requested_type",
        "requested_github_id",
        "team_slug",
        "as_code_owner",
        "linked_github_id",
        "author_name",
        "author_email",
        "email_automation_reason",
        "pr_state",
        "pr_created_at",
        "pr_merged_at",
        "pr_closed_at",
        "base_ref",
        "targets_default_branch",
        "merge_commit_oid",
        "additions",
        "deletions",
        "changed_files",
    ],
}


@pytest.fixture
def seed(migrated_database: Path) -> Iterator[Seed]:
    with connect(migrated_database) as connection:
        yield Seed(connection)


def rows(seed: Seed, sql: str) -> list[tuple[object, ...]]:
    return [tuple(row) for row in seed.connection.exec_driver_sql(sql).fetchall()]


def equal_columns(view: str, table: list[tuple[object, ...]]) -> list[tuple[str, str]]:
    """The pairs of ``view``'s columns that hold equal values in every row of ``table``: a view
    that read one of the pair from the other's source would return the same rows."""
    names = COLUMNS[view]
    return [
        (names[i], names[j])
        for i, j in itertools.combinations(range(len(names)), 2)
        if all(row[i] == row[j] for row in table)
    ]


def test_equal_columns_names_only_the_pairs_equal_in_every_row() -> None:
    names = COLUMNS["v_github_users"]
    first = list(range(len(names)))
    second = list(range(100, 100 + len(names)))
    first[3] = first[5]  # type and created_at, equal in both rows
    second[3] = second[5]
    first[1] = first[2]  # node_id and login, equal in one row only
    assert equal_columns("v_github_users", [tuple(first), tuple(second)]) == [
        ("type", "created_at")
    ]


def github_id(seed: Seed, user_id: int) -> int:
    ((value,),) = rows(seed, f"SELECT github_id FROM github_users WHERE id = {user_id}")
    return int(str(value))


class TestShape:
    def test_every_view_lists_its_columns(self, head_template: Path) -> None:
        assert set(views(head_template)) == set(COLUMNS)
        for view, expected in COLUMNS.items():
            assert [
                row[1] for row in query(head_template, f"PRAGMA table_info({view})")
            ] == expected

    def test_views_identify_rows_by_consumer_keys_not_row_ids(self) -> None:
        """Row ids are each table's ``id`` and the foreign keys to it; they change on a rebuild."""
        row_ids = {"id"} | {
            foreign_key.parent.name
            for table in Base.metadata.tables.values()
            for foreign_key in table.foreign_keys
        }
        assert {"id", "pr_id", "author_id", "reviewer_id", "account_id", "editor_id"} <= row_ids
        for view, columns in COLUMNS.items():
            assert row_ids.isdisjoint(columns), view

    def test_no_view_value_is_a_row_id(self, seed: Seed) -> None:
        """Row ids set far from every GitHub id and count, so a row id a view projects under
        any name shows up as a value."""
        ids = iter(range(7_000_000_001, 7_000_001_000))
        author = seed.user("author", id=next(ids))
        editor = seed.user("editor", id=next(ids))
        seed.profile(author, company="c")
        repository = seed.repository(REPO, id=next(ids))
        pr = seed.pull_request(
            repository,
            1,
            author,
            id=next(ids),
            state="MERGED",
            merged_at=T0,
            merged_by_id=author,
            editor_id=editor,
        )
        seed.review(pr, seed.next_id(), author, "APPROVED", id=next(ids), editor_id=editor)
        seed.comment(pr, author, id=next(ids), editor_id=editor)
        seed.commit(pr, "a" * 40, author)
        seed.review_request(pr, 0, account_id=author, requested_type="User")
        seed.assignee(pr, author)
        seed.sync_run(id=next(ids))
        row_ids = set(range(7_000_000_001, next(ids)))
        # Every id drawn is a row id the file holds.
        assert {
            value
            for table in (
                "github_users",
                "repositories",
                "pull_requests",
                "pr_reviews",
                "pr_comments",
                "sync_runs",
            )
            for (value,) in rows(seed, f"SELECT id FROM {table}")
        } == row_ids
        read = {view: rows(seed, f"SELECT * FROM {view}") for view in COLUMNS}
        # The rows compared: one per seeded account, profile, review, comment, and commit, and
        # one per relationship of the pull request (author, merger, editor, reviewer, review
        # editor, commenter, comment editor, commit author, requested reviewer, assignee).
        assert {view: len(table) for view, table in read.items()} == {
            "v_github_users": 2,
            "v_account_profiles": 1,
            "v_pr_reviews": 1,
            "v_pr_comments": 1,
            "v_commit_authors": 1,
            "v_pr_relationships": 10,
        }
        for view, table in read.items():
            values = {value for row in table for value in row}
            assert values.isdisjoint(row_ids), (view, values & row_ids)


class Scenario:
    """One pull request with every kind of participant plan Step 3b-1 lists."""

    def __init__(self, seed: Seed) -> None:
        self.seed = seed
        self.author = seed.user("author")
        self.bot = seed.user("dependabot", type="Bot")
        self.listed = seed.user("prebidjs-release")
        seed.automation_account(github_id(seed, self.listed), "prebidjs-release", "releases")
        self.reviewer = seed.user("reviewer")
        self.enterprise = seed.user(
            "someone_corp", type="EnterpriseUserAccount", node_id="EUA_1", linked_user_github_id=7
        )
        self.repository = seed.repository(REPO, github_id=39321023)
        self.pr = seed.pull_request(
            self.repository,
            13481,
            self.author,
            github_id=4891210785,
            state="MERGED",
            merged_at=T0,
            closed_at=T0,
            merged_by_id=self.reviewer,
            merged_by_reported=True,
            merge_commit_oid="m" * 40,
            additions=10,
            deletions=2,
            changed_files=3,
        )


class TestReviewsAndComments:
    def test_every_review_is_a_row_with_its_columns(self, seed: Seed) -> None:
        s = Scenario(seed)
        base = seed.next_id() * 100
        for offset, reviewer in enumerate((s.author, s.bot, s.listed, None, s.enterprise)):
            seed.review(s.pr, base + offset, reviewer, "APPROVED")
        assert rows(
            seed,
            "SELECT review_github_id, reviewer_login, reviewer_type, reviewer_node_id, "
            "reviewer_automation_reason, is_pr_author, reviewer_reported "
            "FROM v_pr_reviews ORDER BY review_github_id",
        ) == [
            (base, "author", "User", f"U_{github_id(seed, s.author)}", None, 1, 1),
            (base + 1, "dependabot", "Bot", f"U_{github_id(seed, s.bot)}", None, 0, 1),
            (
                base + 2,
                "prebidjs-release",
                "User",
                f"U_{github_id(seed, s.listed)}",
                "releases",
                0,
                1,
            ),
            (base + 3, None, None, None, None, None, 0),
            (base + 4, "someone_corp", "EnterpriseUserAccount", "EUA_1", None, 0, 1),
        ]
        # The enterprise account is told by its node id: it has no github_id.
        assert rows(
            seed, "SELECT reviewer_github_id FROM v_pr_reviews WHERE reviewer_node_id = 'EUA_1'"
        ) == [(None,)]

    def test_a_review_carries_its_pull_requests_keys_and_its_own_fields(self, seed: Seed) -> None:
        s = Scenario(seed)
        seed.review(
            s.pr,
            3886287882,
            s.reviewer,
            "COMMENTED",
            submitted_at=T0,
            commit_oid="c" * 40,
            inline_comments_total=4,
            editor_id=s.author,
            last_edited_at=T0,
        )
        ((row,),) = [
            rows(
                seed,
                "SELECT repository_github_id, repository_full_name, repository_is_tracked, "
                "pr_number, pr_github_id, state, commit_oid, inline_comments_total, "
                "editor_login FROM v_pr_reviews",
            )
        ]
        assert row == (39321023, REPO, 1, 13481, 4891210785, "COMMENTED", "c" * 40, 4, "author")

    def test_every_comment_is_a_row_and_a_commenter_who_reviewed_is_marked(
        self, seed: Seed
    ) -> None:
        s = Scenario(seed)
        seed.review(s.pr, seed.next_id(), s.reviewer, "COMMENTED")
        for commenter in (s.author, s.reviewer, s.bot, s.listed, None):
            seed.comment(s.pr, commenter)
        seed.comment(s.pr, s.reviewer, created_via_email=True)
        assert sorted(
            rows(
                seed,
                "SELECT author_login, author_type, author_automation_reason, is_pr_author, "
                "author_also_reviewed, created_via_email FROM v_pr_comments",
            ),
            key=repr,
        ) == sorted(
            [
                ("author", "User", None, 1, 0, 0),
                ("reviewer", "User", None, 0, 1, 0),
                ("reviewer", "User", None, 0, 1, 1),
                ("dependabot", "Bot", None, 0, 0, 0),
                ("prebidjs-release", "User", "releases", 0, 0, 0),
                (None, None, None, None, None, 0),
            ],
            key=repr,
        )

    def test_author_also_reviewed_reads_its_own_pull_request(self, seed: Seed) -> None:
        s = Scenario(seed)
        other = seed.pull_request(s.repository, 2, None)
        seed.review(other, seed.next_id(), s.reviewer, "APPROVED")
        seed.comment(s.pr, s.reviewer)
        seed.comment(other, s.reviewer)
        assert rows(
            seed,
            "SELECT pr_number, author_also_reviewed, is_pr_author FROM v_pr_comments "
            "ORDER BY pr_number",
        ) == [(2, 1, None), (13481, 0, 0)]


class TestAccounts:
    def test_every_account_with_its_type_and_reason_side_by_side(self, seed: Seed) -> None:
        seed.user("person", github_id=583231, node_id="MDQ6VXNlcjU4MzIzMQ==", name="P")
        seed.user("dependabot", type="Bot", github_id=49699333, node_id="MDM6Qm90NDk2OTkzMzM=")
        listed = seed.user("prebidjs-release", github_id=99054218, node_id="U_listed")
        seed.automation_account(github_id(seed, listed), "prebidjs-release", "releases")
        seed.user("gone", type="Mannequin", not_found_at=T0, claimant_github_id=5)
        seed.user("someone_corp", type="EnterpriseUserAccount", node_id="EUA_1")
        assert sorted(
            rows(
                seed,
                "SELECT login, type, automation_reason, not_found_at IS NOT NULL, "
                "claimant_github_id, github_id IS NULL FROM v_github_users",
            )
        ) == [
            ("dependabot", "Bot", None, 0, None, 0),
            ("gone", "Mannequin", None, 1, 5, 0),
            ("person", "User", None, 0, None, 0),
            ("prebidjs-release", "User", "releases", 0, None, 0),
            ("someone_corp", "EnterpriseUserAccount", None, 0, None, 1),
        ]

    def test_a_profile_row_with_its_accounts_keys(self, seed: Seed) -> None:
        user = seed.user("muuki88", github_id=647727, node_id="MDQ6VXNlcjY0NzcyNw==")
        organization = seed.user("prebid", type="Organization", github_id=13242760)
        seed.user("no-profile")
        seed.automation_account(13242760, "prebid", "organization")
        seed.profile(user, company="gutefrage.net", location="Munich")
        seed.profile(
            organization,
            description="Prebid",
            email="info@prebid.org",
            is_verified=False,
            has_sponsors_listing=False,
            pinned_items_remaining=6,
        )
        assert rows(
            seed,
            "SELECT login, automation_reason FROM v_account_profiles ORDER BY login",
        ) == [("muuki88", None), ("prebid", "organization")]
        assert rows(
            seed,
            "SELECT github_id, node_id, login, type, company, location, email, "
            "pinned_items_remaining FROM v_account_profiles ORDER BY login",
        ) == [
            (
                647727,
                "MDQ6VXNlcjY0NzcyNw==",
                "muuki88",
                "User",
                "gutefrage.net",
                "Munich",
                None,
                None,
            ),
            (13242760, "U_13242760", "prebid", "Organization", None, None, "info@prebid.org", 6),
        ]


class TestCommitAuthors:
    def test_the_linked_account_and_the_email_reason_side_by_side(self, seed: Seed) -> None:
        person = seed.user("person")
        bot = seed.user("dependabot[bot]", type="Bot")
        listed = seed.user("claude")
        seed.automation_account(github_id(seed, listed), "claude", "agent")
        seed.automation_email("noreply@anthropic.com", "agent email")
        pr = seed.pull_request(seed.repository(REPO), 3, person, github_id=33)
        seed.commit(pr, "1" * 40, person, linked_github_id=github_id(seed, person))
        seed.commit(pr, "2" * 40, bot, linked_github_id=github_id(seed, bot))
        seed.commit(pr, "3" * 40, listed, linked_github_id=github_id(seed, listed))
        seed.commit(pr, "4" * 40, None, author_email="NoReply@Anthropic.com")
        seed.commit(pr, "5" * 40, None, author_email=None)
        seed.commit(pr, "6" * 40, person, author_email="noreply@anthropic.com")
        assert rows(
            seed,
            "SELECT sha, account_login, account_type, account_automation_reason, "
            "email_automation_reason FROM v_commit_authors ORDER BY sha",
        ) == [
            ("1" * 40, "person", "User", None, None),
            ("2" * 40, "dependabot[bot]", "Bot", None, None),
            ("3" * 40, "claude", "User", "agent", None),
            ("4" * 40, None, None, None, "agent email"),
            ("5" * 40, None, None, None, None),
            ("6" * 40, "person", "User", None, "agent email"),
        ]


class TestRelationships:
    def relationships(self, seed: Seed, columns: str, where: str = "1") -> list[tuple[object, ...]]:
        return sorted(
            rows(seed, f"SELECT {columns} FROM v_pr_relationships WHERE {where}"), key=repr
        )

    def test_one_row_per_source_row(self, seed: Seed) -> None:
        s = Scenario(seed)
        seed.review(s.pr, seed.next_id(), s.reviewer, "APPROVED")
        seed.review(s.pr, seed.next_id(), None, "COMMENTED", last_edited_at=T0)
        seed.comment(s.pr, s.bot)
        seed.comment(s.pr, None, editor_id=s.author, last_edited_at=T0)
        seed.review_request(s.pr, 0, account_id=s.listed, requested_type="User")
        seed.assignee(s.pr, s.author)
        seed.commit(s.pr, "a" * 40, s.author)
        counts = {
            row[0]: row[1]
            for row in rows(
                seed, "SELECT relationship, COUNT(*) FROM v_pr_relationships GROUP BY 1"
            )
        }
        assert counts == {
            "author": 1,
            "merged_by": 1,
            "review": 2,
            "comment": 2,
            "review_request": 1,
            "assignee": 1,
            "commit_author": 1,
            "review_editor": 1,
            "comment_editor": 1,
        }
        # The PR's size rides on every row.
        assert set(
            rows(
                seed,
                "SELECT pr_state, merge_commit_oid, additions, deletions, changed_files "
                "FROM v_pr_relationships",
            )
        ) == {("MERGED", "m" * 40, 10, 2, 3)}

    def test_an_open_pull_request_has_no_merger_row_and_a_null_author_keeps_its_row(
        self, seed: Seed
    ) -> None:
        pr = seed.pull_request(seed.repository(REPO), 9, None)
        assert pr
        assert self.relationships(seed, "relationship, account_login, reported") == [
            ("author", None, 0)
        ]

    def test_a_merger_github_returned_as_null_keeps_its_row(self, seed: Seed) -> None:
        seed.pull_request(seed.repository(REPO), 9, None, state="MERGED", merged_at=T0)
        assert self.relationships(
            seed, "relationship, account_login", "relationship = 'merged_by'"
        ) == [("merged_by", None)]

    def test_requests_of_a_team_and_with_no_reviewer_are_told_apart(self, seed: Seed) -> None:
        s = Scenario(seed)
        seed.review_request(
            s.pr,
            0,
            request_github_id=3843805356,
            request_node_id="RR_user",
            requested_type="User",
            requested_github_id=github_id(seed, s.reviewer),
            requested_node_id=f"U_{github_id(seed, s.reviewer)}",
            account_id=s.reviewer,
        )
        seed.review_request(
            s.pr,
            1,
            request_github_id=77,
            request_node_id="RR_team",
            requested_type="Team",
            requested_github_id=5,
            requested_node_id="T_5",
            team_slug="core",
            as_code_owner=True,
        )
        seed.review_request(s.pr, 2, request_github_id=None, request_node_id="RR_none")
        assert self.relationships(
            seed,
            "request_ordinal, request_node_id, request_github_id, requested_type, "
            "requested_github_id, team_slug, as_code_owner, account_login",
            "relationship = 'review_request'",
        ) == [
            (0, "RR_user", 3843805356, "User", github_id(seed, s.reviewer), None, 0, "reviewer"),
            (1, "RR_team", 77, "Team", 5, "core", 1, None),
            (2, "RR_none", None, None, None, None, 0, None),
        ]

    def test_commit_links_are_told_apart(self, seed: Seed) -> None:
        """No link, a link whose account no stored row has (its lookup returned NOT_FOUND), and a
        link to a stored account a later lookup returned NOT_FOUND for."""
        s = Scenario(seed)
        kept = seed.user("kept", not_found_at=T0)
        seed.commit(s.pr, "1" * 40, None, linked_github_id=None)
        seed.commit(s.pr, "2" * 40, None, linked_github_id=987654)
        seed.commit(s.pr, "3" * 40, kept, linked_github_id=github_id(seed, kept))
        assert self.relationships(
            seed,
            "commit_sha, linked_github_id, account_login, account_not_found_at IS NOT NULL",
            "relationship = 'commit_author'",
        ) == [
            ("1" * 40, None, None, 0),
            ("2" * 40, 987654, None, 0),
            ("3" * 40, github_id(seed, kept), "kept", 1),
        ]

    def test_an_edit_with_no_editor_is_kept_as_an_editor_row(self, seed: Seed) -> None:
        pr = seed.pull_request(seed.repository(REPO), 9, None, last_edited_at=T0)
        seed.comment(pr, None, github_id=55, last_edited_at=T0)
        seed.comment(pr, None, github_id=56)
        assert self.relationships(
            seed,
            "relationship, comment_github_id, account_login",
            "relationship LIKE '%editor'",
        ) == [("comment_editor", 55, None), ("pr_editor", None, None)]

    def test_a_kept_reference_carries_reported_false(self, seed: Seed) -> None:
        s = Scenario(seed)
        seed.review(s.pr, 3886287882, s.reviewer, "APPROVED", reviewer_reported=False)
        seed.comment(s.pr, s.bot, github_id=66, author_reported=False)
        assert self.relationships(
            seed,
            "relationship, account_login, reported",
            "relationship IN ('review', 'comment')",
        ) == [("comment", "dependabot", 0), ("review", "reviewer", 0)]

    def test_a_commit_an_automation_email_matches(self, seed: Seed) -> None:
        s = Scenario(seed)
        seed.automation_email("noreply@anthropic.com", "agent email")
        seed.commit(s.pr, "1" * 40, None, author_email="NoReply@Anthropic.com", author_name="C")
        assert self.relationships(
            seed,
            "author_name, author_email, email_automation_reason",
            "relationship = 'commit_author'",
        ) == [("C", "NoReply@Anthropic.com", "agent email")]

    def test_the_review_row_carries_its_state_and_submission_time(self, seed: Seed) -> None:
        s = Scenario(seed)
        seed.review(s.pr, 3886287882, s.reviewer, "DISMISSED", submitted_at=T0)
        assert self.relationships(
            seed,
            "review_github_id, review_state, review_submitted_at",
            "relationship = 'review'",
        ) == [(3886287882, "DISMISSED", "2026-01-01 00:00:00.000000")]


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def stored(seconds: int) -> str:
    """How a timestamp ``seconds`` after T0 reads back from a view."""
    return at(seconds).strftime("%Y-%m-%d %H:%M:%S.%f")


class TestEveryColumn:
    """Each view's whole rows, every source column seeded with a value no other column holds,
    so a column read from the wrong source shows."""

    def test_v_github_users(self, seed: Seed) -> None:
        seed.user(
            "person",
            github_id=583231,
            node_id="U_person",
            name="P Erson",
            created_at=at(1),
            updated_at=at(2),
            refreshed_at=at(3),
            not_found_at=at(4),
        )
        seed.automation_account(583231, "person", "listed")
        for number in (1, 2):
            seed.user(
                f"corp_{number}",
                type="EnterpriseUserAccount",
                node_id=f"EUA_{number}",
                name=f"Corp {number}",
                created_at=at(5),
                updated_at=at(6),
                linked_user_github_id=7 + number,
                refreshed_at=at(7),
            )
        seed.user(
            "imported",
            type="Mannequin",
            github_id=501,
            node_id="MQ_1",
            claimant_github_id=5,
            refreshed_at=at(8),
        )
        table = rows(seed, "SELECT * FROM v_github_users ORDER BY node_id")
        assert equal_columns("v_github_users", table) == []
        assert table == [
            (
                None,
                "EUA_1",
                "corp_1",
                "EnterpriseUserAccount",
                "Corp 1",
                stored(5),
                stored(6),
                8,
                None,
                stored(7),
                None,
                None,
            ),
            (
                None,
                "EUA_2",
                "corp_2",
                "EnterpriseUserAccount",
                "Corp 2",
                stored(5),
                stored(6),
                9,
                None,
                stored(7),
                None,
                None,
            ),
            (
                501,
                "MQ_1",
                "imported",
                "Mannequin",
                None,
                None,
                None,
                None,
                5,
                stored(8),
                None,
                None,
            ),
            (
                583231,
                "U_person",
                "person",
                "User",
                "P Erson",
                stored(1),
                stored(2),
                None,
                None,
                stored(3),
                stored(4),
                "listed",
            ),
        ]

    def test_v_account_profiles(self, seed: Seed) -> None:
        user = seed.user("muuki88", github_id=647727, node_id="U_muuki88")
        organization = seed.user("prebid", type="Organization", github_id=13242760, node_id="O_1")
        seed.automation_account(13242760, "prebid", "organization")
        seed.profile(
            user,
            company="gutefrage.net",
            location="Munich",
            website_url="https://mukis.de",
            refreshed_at=at(1),
        )
        seed.profile(
            organization,
            location="Earth",
            website_url="www.prebid.org",
            description="Prebid",
            email="info@prebid.org",
            twitter_username="prebid_org",
            is_verified=True,
            archived_at=at(2),
            has_sponsors_listing=False,
            pinned_items_remaining=6,
            refreshed_at=at(3),
        )
        table = rows(seed, "SELECT * FROM v_account_profiles ORDER BY login")
        assert equal_columns("v_account_profiles", table) == []
        assert table == [
            (
                647727,
                "U_muuki88",
                "muuki88",
                "User",
                None,
                "gutefrage.net",
                "Munich",
                "https://mukis.de",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                stored(1),
            ),
            (
                13242760,
                "O_1",
                "prebid",
                "Organization",
                "organization",
                None,
                "Earth",
                "www.prebid.org",
                "Prebid",
                "info@prebid.org",
                "prebid_org",
                1,
                stored(2),
                0,
                6,
                stored(3),
            ),
        ]

    def participants(self, seed: Seed) -> dict[str, int]:
        accounts = {
            "author": seed.user("author", github_id=11, node_id="U_author"),
            "bot": seed.user("bot", type="Bot", github_id=22, node_id="B_bot"),
            "editor": seed.user("editor", github_id=33, node_id="U_editor"),
        }
        seed.automation_account(22, "bot", "bot reason")
        seed.automation_account(33, "editor", "editor reason")
        accounts["repository"] = seed.repository(REPO, github_id=39321023)
        accounts["pr"] = seed.pull_request(
            accounts["repository"], 13481, accounts["author"], github_id=4891210785
        )
        return accounts

    def test_v_pr_reviews(self, seed: Seed) -> None:
        a = self.participants(seed)
        seed.review(
            a["pr"],
            3886287882,
            a["bot"],
            "DISMISSED",
            node_id="PRR_one",
            reviewer_reported=False,
            created_at=at(1),
            submitted_at=at(2),
            published_at=at(3),
            updated_at=at(4),
            last_edited_at=at(5),
            editor_id=a["editor"],
            includes_created_edit=True,
            commit_oid="c" * 40,
            author_association="CONTRIBUTOR",
            author_can_push_to_repository=False,
            inline_comments_total=4,
        )
        authorless = seed.pull_request(a["repository"], 9, None, github_id=99)
        seed.review(
            authorless,
            7,
            a["author"],
            "APPROVED",
            node_id="PRR_two",
            submitted_at=None,
            reviewer_reported=False,
        )
        keys = (39321023, REPO, 1)
        table = rows(seed, "SELECT * FROM v_pr_reviews ORDER BY review_github_id")
        assert equal_columns("v_pr_reviews", table) == []
        assert table == [
            (
                *keys,
                9,
                99,
                7,
                "PRR_two",
                "APPROVED",
                stored(0),
                None,
                None,
                stored(0),
                None,
                0,
                None,
                "MEMBER",
                1,
                0,
                0,
                11,
                "U_author",
                "author",
                "User",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
            (
                *keys,
                13481,
                4891210785,
                3886287882,
                "PRR_one",
                "DISMISSED",
                stored(1),
                stored(2),
                stored(3),
                stored(4),
                stored(5),
                1,
                "c" * 40,
                "CONTRIBUTOR",
                0,
                4,
                0,
                22,
                "B_bot",
                "bot",
                "Bot",
                "bot reason",
                33,
                "U_editor",
                "editor",
                "User",
                "editor reason",
                0,
            ),
        ]

    def test_v_pr_comments(self, seed: Seed) -> None:
        a = self.participants(seed)
        seed.review(a["pr"], 1, a["bot"], "COMMENTED")
        seed.comment(
            a["pr"],
            a["bot"],
            3047425183,
            node_id="IC_one",
            author_reported=False,
            created_at=at(1),
            published_at=at(2),
            updated_at=at(3),
            last_edited_at=at(4),
            editor_id=a["editor"],
            created_via_email=True,
            includes_created_edit=True,
            author_association="NONE",
        )
        seed.comment(a["pr"], a["author"], 5, node_id="IC_two", includes_created_edit=True)
        seed.comment(a["pr"], a["editor"], 6, node_id="IC_three", created_via_email=True)
        keys = (39321023, REPO, 1, 13481, 4891210785)
        table = rows(seed, "SELECT * FROM v_pr_comments ORDER BY comment_github_id")
        assert equal_columns("v_pr_comments", table) == []
        assert table == [
            (
                *keys,
                5,
                "IC_two",
                stored(0),
                None,
                stored(0),
                None,
                0,
                1,
                "MEMBER",
                1,
                11,
                "U_author",
                "author",
                "User",
                None,
                None,
                None,
                None,
                None,
                None,
                1,
                0,
            ),
            (
                *keys,
                6,
                "IC_three",
                stored(0),
                None,
                stored(0),
                None,
                1,
                0,
                "MEMBER",
                1,
                33,
                "U_editor",
                "editor",
                "User",
                "editor reason",
                None,
                None,
                None,
                None,
                None,
                0,
                0,
            ),
            (
                *keys,
                3047425183,
                "IC_one",
                stored(1),
                stored(2),
                stored(3),
                stored(4),
                1,
                1,
                "NONE",
                0,
                22,
                "B_bot",
                "bot",
                "Bot",
                "bot reason",
                33,
                "U_editor",
                "editor",
                "User",
                "editor reason",
                0,
                1,
            ),
        ]

    def test_v_commit_authors(self, seed: Seed) -> None:
        a = self.participants(seed)
        seed.automation_email("noreply@anthropic.com", "email reason")
        seed.commit(
            a["pr"],
            "a" * 40,
            a["bot"],
            linked_github_id=22,
            author_name="Dev",
            author_email="NoReply@Anthropic.com",
            authored_at=at(1),
        )
        seed.commit(a["pr"], "b" * 40, None, linked_github_id=987654)
        table = rows(seed, "SELECT * FROM v_commit_authors ORDER BY sha")
        assert equal_columns("v_commit_authors", table) == []
        assert table == [
            (
                39321023,
                REPO,
                1,
                13481,
                4891210785,
                "a" * 40,
                22,
                "Dev",
                "NoReply@Anthropic.com",
                stored(1),
                22,
                "B_bot",
                "bot",
                "Bot",
                "bot reason",
                "email reason",
            ),
            (
                39321023,
                REPO,
                1,
                13481,
                4891210785,
                "b" * 40,
                987654,
                "A Developer",
                "dev@example.com",
                stored(0),
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        ]


COLUMNS_OF_RELATIONSHIPS = COLUMNS["v_pr_relationships"]


def relationship(**values: object) -> tuple[object, ...]:
    """A v_pr_relationships row of the merged pull request below, in column order."""
    row: dict[str, object] = dict.fromkeys(COLUMNS_OF_RELATIONSHIPS)
    row.update(
        repository_github_id=39321023,
        repository_full_name=REPO,
        repository_is_tracked=1,
        pr_number=13481,
        pr_github_id=4891210785,
        pr_state="MERGED",
        pr_created_at=stored(1),
        pr_merged_at=stored(2),
        pr_closed_at=stored(3),
        base_ref="main",
        targets_default_branch=0,
        merge_commit_oid="m" * 40,
        additions=10,
        deletions=2,
        changed_files=3,
    )
    for name, value in values.items():
        assert name in row, name
        row[name] = value
    return tuple(row[name] for name in COLUMNS_OF_RELATIONSHIPS)


def account_columns(github_id: int, node_id: str, login: str, type_: str, reason: str | None):
    return {
        "account_github_id": github_id,
        "account_node_id": node_id,
        "account_login": login,
        "account_type": type_,
        "account_automation_reason": reason,
    }


class TestEveryRelationshipColumn:
    def test_every_relationship_row_whole(self, seed: Seed) -> None:
        author = seed.user("author", github_id=11, node_id="U_author", not_found_at=at(9))
        merger = seed.user("merger", github_id=22, node_id="U_merger")
        bot = seed.user("bot", type="Bot", github_id=33, node_id="B_bot")
        editor = seed.user("editor", github_id=44, node_id="U_editor")
        seed.automation_account(33, "bot", "bot reason")
        seed.automation_email("noreply@anthropic.com", "email reason")
        repository = seed.repository(REPO, github_id=39321023, default_branch="master")
        pr = seed.pull_request(
            repository,
            13481,
            author,
            github_id=4891210785,
            author_reported=False,
            created_at=at(1),
            state="MERGED",
            merged_at=at(2),
            closed_at=at(3),
            merged_by_id=merger,
            merged_by_reported=True,
            base_ref="main",
            targets_default_branch=False,
            merge_commit_oid="m" * 40,
            additions=10,
            deletions=2,
            changed_files=3,
            editor_id=editor,
        )
        seed.review(pr, 101, bot, "CHANGES_REQUESTED", submitted_at=at(4), editor_id=editor)
        seed.comment(pr, bot, 202, author_reported=False, editor_id=editor)
        seed.review_request(
            pr,
            0,
            request_github_id=303,
            request_node_id="RR_one",
            requested_type="User",
            requested_github_id=22,
            requested_node_id="U_merger",
            account_id=merger,
            as_code_owner=True,
        )
        seed.assignee(pr, bot)
        seed.commit(
            pr,
            "a" * 40,
            author,
            linked_github_id=11,
            author_name="Dev",
            author_email="NoReply@Anthropic.com",
        )
        author_account = {
            **account_columns(11, "U_author", "author", "User", None),
            "account_not_found_at": stored(9),
        }
        merger_account = account_columns(22, "U_merger", "merger", "User", None)
        bot_account = account_columns(33, "B_bot", "bot", "Bot", "bot reason")
        editor_account = account_columns(44, "U_editor", "editor", "User", None)
        table = rows(seed, "SELECT * FROM v_pr_relationships ORDER BY relationship")
        assert equal_columns("v_pr_relationships", table) == []
        assert table == [
            relationship(relationship="assignee", **bot_account),
            relationship(relationship="author", reported=0, **author_account),
            relationship(relationship="comment", comment_github_id=202, reported=0, **bot_account),
            relationship(relationship="comment_editor", comment_github_id=202, **editor_account),
            relationship(
                relationship="commit_author",
                commit_sha="a" * 40,
                linked_github_id=11,
                author_name="Dev",
                author_email="NoReply@Anthropic.com",
                email_automation_reason="email reason",
                **author_account,
            ),
            relationship(relationship="merged_by", reported=1, **merger_account),
            relationship(relationship="pr_editor", **editor_account),
            relationship(
                relationship="review",
                review_github_id=101,
                reported=1,
                review_state="CHANGES_REQUESTED",
                review_submitted_at=stored(4),
                **bot_account,
            ),
            relationship(relationship="review_editor", review_github_id=101, **editor_account),
            relationship(
                relationship="review_request",
                request_node_id="RR_one",
                request_github_id=303,
                request_ordinal=0,
                requested_type="User",
                requested_github_id=22,
                as_code_owner=1,
                **merger_account,
            ),
        ]


def _one_account_on_every_pull_request(database: Path, prs: int) -> None:
    """A maintainer who reviews and comments on every PR, as on a repository with few
    maintainers, and one other commenter per PR."""
    with connect(database) as connection:
        seed = Seed(connection)
        repository = seed.repository(REPO)
        maintainer, other = seed.user("maintainer"), seed.user("other")
        for number in range(1, prs + 1):
            pr = seed.pull_request(repository, number)
            seed.review(pr, seed.next_id(), maintainer, "APPROVED")
            seed.comment(pr, maintainer)
            seed.comment(pr, other)


def vm_steps(database: Path, sql: str) -> int:
    """SQLite virtual-machine instructions the query runs, in hundreds: its work, which unlike
    its time does not depend on the machine."""
    steps = 0

    def count() -> int:
        nonlocal steps
        steps += 1
        return 0

    with closing(sqlite3.connect(database)) as connection:
        connection.set_progress_handler(count, 100)
        connection.execute(sql).fetchall()
    return steps


class TestConcentratedParticipation:
    """Four times the PRs costs v_pr_comments about four times the work, not sixteen times:
    ``author_also_reviewed`` is an index lookup on (PR, account) (plan 5.1). With one index per
    foreign-key column instead, the planner took the account's and read every row of the
    account for each PR (observed on SQLite 3.45.3 and 3.47.1)."""

    SIZES = (100, 400)

    @staticmethod
    def rows_read(prs: int) -> dict[str, int]:
        """The rows each view returns for ``prs`` pull requests of the seed loop: a review and
        two comments on each, and in v_pr_relationships an author row for each as well."""
        return {
            "v_pr_reviews": prs,
            "v_pr_comments": 2 * prs,
            "v_pr_relationships": prs + prs + 2 * prs,
        }

    @pytest.fixture(scope="class")
    def databases(
        self, head_template: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> dict[int, Path]:
        folder = tmp_path_factory.mktemp("concentrated")
        built = {}
        for prs in self.SIZES:
            built[prs] = folder / f"{prs}.db"
            shutil.copyfile(head_template, built[prs])
            _one_account_on_every_pull_request(built[prs], prs)
            built[prs].chmod(0o444)
        return built

    def growth(self, databases: dict[int, Path], view: str) -> float:
        work = [vm_steps(databases[prs], f"SELECT * FROM {view}") for prs in self.SIZES]
        return work[1] / work[0]

    @pytest.mark.parametrize("view", ["v_pr_reviews", "v_pr_comments", "v_pr_relationships"])
    def test_work_grows_with_the_pull_requests(self, databases: dict[int, Path], view: str) -> None:
        for prs in self.SIZES:
            assert query(databases[prs], f"SELECT COUNT(*) FROM {view}") == [
                (self.rows_read(prs)[view],)
            ], prs
        assert self.growth(databases, view) < 6

    def test_a_lookup_by_the_account_alone_is_reported(
        self, databases: dict[int, Path], tmp_path: Path
    ) -> None:
        """With single-column indexes in place of the (PR, account) one, the same measure grows
        with the square of the pull requests."""
        altered = {}
        for prs in self.SIZES:
            altered[prs] = tmp_path / f"{prs}.db"
            shutil.copyfile(databases[prs], altered[prs])
            with closing(sqlite3.connect(altered[prs])) as connection, connection:
                for statement in SINGLE_COLUMN_INDEXES:
                    connection.execute(statement)
        assert self.growth(altered, "v_pr_comments") > 10

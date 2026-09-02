"""Tests for the grouping metric.

Line recall cannot see a grouping mistake - the words are all still there, just
in the wrong block. This metric is what makes the failure CLAUDE.md section 1
ranks highest visible at all, so it is checked against known answers.
"""
from overlayr.eval.dataset import parse_ground_truth_groups
from overlayr.eval.metrics import group_separation


class TestParseGroups:
    def test_blank_lines_separate_groups(self):
        raw = "NO WAY!\n\nI REFUSE\nTO ACCEPT\nTHAT!\n\nACK!"
        assert parse_ground_truth_groups(raw) == [
            ["NO WAY!"], ["I REFUSE", "TO ACCEPT", "THAT!"], ["ACK!"],
        ]

    def test_comments_are_ignored_without_splitting_a_group(self):
        raw = "# the bubble top left\nfirst line\nsecond line"
        assert parse_ground_truth_groups(raw) == [["first line", "second line"]]

    def test_repeated_blank_lines_do_not_make_empty_groups(self):
        assert parse_ground_truth_groups("one\n\n\n\ntwo") == [["one"], ["two"]]

    def test_empty_input(self):
        assert parse_ground_truth_groups("") == []


class TestGroupSeparation:
    def test_blocks_that_respect_the_groups_score_one(self):
        groups = [["miss ayase"], ["the curse"], ["use your powers"]]
        blocks = ["miss ayase", "the curse", "use your powers"]

        assert group_separation(groups, blocks) == 1.0

    def test_a_block_fusing_two_bubbles_is_caught(self):
        groups = [["miss ayase"], ["the curse"], ["use your powers"]]
        blocks = ["miss ayase the curse", "use your powers"]

        assert group_separation(groups, blocks) == 0.5

    def test_splitting_a_group_is_not_penalised(self):
        # Splitting keeps the meaning readable; fusing does not. Only fusing is
        # the failure this metric is here to catch.
        groups = [["i refuse to accept that"], ["ack"]]
        blocks = ["i refuse", "to accept that", "ack"]

        assert group_separation(groups, blocks) == 1.0

    def test_words_shared_between_groups_do_not_count_as_fusion(self):
        groups = [["the quick fox"], ["the lazy dog"]]
        blocks = ["the quick fox"]

        assert group_separation(groups, blocks) == 1.0

    def test_one_group_cannot_be_fused_with_itself(self):
        assert group_separation([["only one group here"]], ["only one group here"]) == 1.0

    def test_no_groups_is_neutral(self):
        assert group_separation([], ["anything"]) == 1.0

    def test_no_blocks_is_neutral(self):
        assert group_separation([["a b c"], ["d e f"]], []) == 1.0

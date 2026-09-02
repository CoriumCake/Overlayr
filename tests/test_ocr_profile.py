"""Tests for per-scene OCR profiles and region normalisation."""
import numpy as np
import pytest

from overlayr.core.scene import SceneType, uses_spell_correction
from overlayr.services.lexicon import dictionary_ratio
from overlayr.services.ocr_profile import (LEGACY_PROFILE, PROFILES,
                                           TARGET_REGION_HEIGHT, OcrProfile,
                                           get_profile, is_light_on_dark,
                                           prepare_region)


class TestProfiles:
    def test_every_scene_has_a_profile(self):
        assert set(PROFILES) == set(SceneType)

    def test_an_unknown_scene_falls_back_to_the_legacy_profile(self):
        assert get_profile(None) is LEGACY_PROFILE

    def test_every_profile_keeps_a_whole_frame_pass(self):
        # The whole-frame pass is the safety net: without it, text the region
        # detector missed would never be read at all.
        for profile in list(PROFILES.values()) + [LEGACY_PROFILE]:
            assert profile.full_frame

    def test_the_legacy_profile_reads_no_regions(self):
        assert LEGACY_PROFILE.uses_regions is False

    def test_each_scene_profile_reads_regions(self):
        for scene in SceneType:
            assert PROFILES[scene].uses_regions

    def test_documents_are_left_alone_the_most(self):
        # Section 6: a page is already high contrast, so the region pass does
        # the least work of the three.
        assert PROFILES[SceneType.DOCUMENT].region == ("baseline",)
        assert PROFILES[SceneType.DOCUMENT].normalize_polarity is False


class TestSpellCorrection:
    def test_comics_are_corrected(self):
        assert uses_spell_correction(SceneType.COMIC) is True

    def test_documents_and_games_are_not(self):
        assert uses_spell_correction(SceneType.DOCUMENT) is False
        assert uses_spell_correction(SceneType.GAME_UI) is False

    def test_an_unknown_scene_keeps_the_old_behaviour(self):
        assert uses_spell_correction(None) is True


class TestPolarity:
    def test_light_text_on_a_dark_panel_is_detected(self):
        panel = np.full((60, 200), 20, np.uint8)
        panel[25:35, 40:160] = 240
        assert is_light_on_dark(panel) is True

    def test_dark_text_on_a_light_page_is_not(self):
        page = np.full((60, 200), 240, np.uint8)
        page[25:35, 40:160] = 20
        assert is_light_on_dark(page) is False

    def test_a_tiny_crop_is_left_alone(self):
        assert is_light_on_dark(np.zeros((3, 3), np.uint8)) is False


class TestPrepareRegion:
    def test_a_light_on_dark_crop_is_inverted(self):
        crop = np.full((60, 200, 3), 20, np.uint8)
        crop[25:35, 40:160] = 240
        profile = OcrProfile(full_frame=("baseline",), normalize_polarity=True,
                             upscale_small_regions=False)

        prepared, scale = prepare_region(crop, profile)

        assert scale == 1.0
        assert prepared[0, 0, 0] > 200  # the dark ground became light

    def test_polarity_is_left_alone_when_the_profile_says_so(self):
        crop = np.full((60, 200, 3), 20, np.uint8)
        crop[25:35, 40:160] = 240
        profile = OcrProfile(full_frame=("baseline",), upscale_small_regions=False)

        prepared, _ = prepare_region(crop, profile)

        assert prepared[0, 0, 0] < 50

    def test_a_short_crop_is_enlarged(self):
        crop = np.full((20, 200, 3), 255, np.uint8)
        profile = OcrProfile(full_frame=("baseline",))

        prepared, scale = prepare_region(crop, profile)

        assert scale > 1.0
        assert prepared.shape[0] >= min(TARGET_REGION_HEIGHT, 20 * 3)

    def test_a_tall_crop_is_left_at_its_size(self):
        crop = np.full((400, 200, 3), 255, np.uint8)
        prepared, scale = prepare_region(crop, OcrProfile(full_frame=("baseline",)))

        assert scale == 1.0
        assert prepared.shape[0] == 400

    def test_an_empty_crop_is_handled(self):
        empty = np.zeros((0, 0, 3), np.uint8)
        prepared, scale = prepare_region(empty, OcrProfile(full_frame=("baseline",)))
        assert scale == 1.0
        assert prepared.size == 0


class TestDictionaryRatio:
    def test_real_words_score_high(self):
        assert dictionary_ratio("ground rules") == pytest.approx(1.0)

    def test_garbled_text_scores_low(self):
        assert dictionary_ratio("ilblltl zzqx") < 0.5

    def test_text_without_latin_words_is_neutral(self):
        # Nothing here can be looked up, so the test must not tip the vote.
        assert dictionary_ratio("日本語") == 0.5
        assert dictionary_ratio("100 200") == 0.5

"""
Feature Gate — Centralized feature access control.

Features are defined as string constants that match Stripe's feature lookup_keys.
The FeatureGate checks if the authenticated user has a specific feature
by reading from the AuthService's cached feature list (synced from Firestore).

Usage:
    gate = FeatureGate(auth_service)
    
    if gate.check(Feature.PREMIUM_AI):
        # Use premium AI model
    else:
        # Use basic AI model
    
    gate.require(Feature.UNLIMITED_TRANSLATIONS)  # Raises if not available
"""


class Feature:
    """
    Feature lookup keys — must match Stripe entitlement feature lookup_keys exactly.
    These are synced: Stripe → Webhook → Firestore → Desktop App.
    """
    ADVANCED_OCR = "advanced_ocr"
    PRIORITY_SUPPORT = "priority_support"
    PREMIUM_AI = "premium_ai"
    NO_WATERMARK = "no_watermark"
    UNLIMITED_TRANSLATIONS = "unlimited_translations"
    ZERO_LATENCY = "zero_latency"

    # ── Tier Bundles (for reference / UI display) ──
    PRO_FEATURES = {ADVANCED_OCR, PRIORITY_SUPPORT}
    PLUS_FEATURES = PRO_FEATURES | {PREMIUM_AI, NO_WATERMARK}
    ULTIMATE_FEATURES = PLUS_FEATURES | {UNLIMITED_TRANSLATIONS, ZERO_LATENCY}

    # Human-readable labels for UI display
    LABELS = {
        ADVANCED_OCR: "Advanced OCR Engine",
        PRIORITY_SUPPORT: "Priority Support",
        PREMIUM_AI: "Premium AI Models",
        NO_WATERMARK: "No Watermark",
        UNLIMITED_TRANSLATIONS: "Unlimited Translations",
        ZERO_LATENCY: "Zero Latency Mode",
    }


class FeatureNotAvailableError(Exception):
    """Raised when a required feature is not available for the current user."""

    def __init__(self, feature_key: str):
        self.feature_key = feature_key
        label = Feature.LABELS.get(feature_key, feature_key)
        super().__init__(
            f"Feature '{label}' requires an active subscription. "
            f"Upgrade at https://overlayr.com/#pricing"
        )


class FeatureGate:
    """
    Central access control for gated features.
    
    Reads the user's active features from AuthService, which syncs
    them from Firestore (written by the Stripe webhook handler).
    
    Security: The features list in Firestore can only be written
    by the admin SDK (Cloud Functions), not by the client.
    """

    def __init__(self, auth_service):
        self._auth = auth_service

    def check(self, feature_key: str) -> bool:
        """
        Check if the user has access to a specific feature.
        Returns True if available, False otherwise.
        """
        return self._auth.has_feature(feature_key)

    def require(self, feature_key: str) -> None:
        """
        Assert that the user has access to a feature.
        Raises FeatureNotAvailableError if not.
        """
        if not self.check(feature_key):
            raise FeatureNotAvailableError(feature_key)

    def get_active_features(self) -> list[str]:
        """Get list of all active feature keys for the current user."""
        return list(self._auth.active_features)

    def get_active_labels(self) -> list[str]:
        """Get human-readable labels for all active features."""
        return [
            Feature.LABELS.get(f, f)
            for f in self._auth.active_features
        ]

    def get_tier_name(self) -> str:
        """
        Determine the effective tier name based on active features.
        Returns 'Ultimate', 'Plus', 'Pro', or 'Free'.
        """
        features = set(self._auth.active_features)
        if features >= Feature.ULTIMATE_FEATURES:
            return "Ultimate"
        elif features >= Feature.PLUS_FEATURES:
            return "Plus"
        elif features >= Feature.PRO_FEATURES:
            return "Pro"
        return "Free"

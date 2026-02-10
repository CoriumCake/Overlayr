"""
Stripe Feature & Entitlement Setup Script
==========================================
Run this once to create Features in Stripe and attach them to Products.

Usage:
  python scripts/setup_stripe_features.py
"""

import os
import stripe
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

if not stripe.api_key:
    print("ERROR: Set STRIPE_SECRET_KEY in your .env file.")
    print("  STRIPE_SECRET_KEY=sk_test_...")
    exit(1)

# ── Feature Definitions ──────────────────────────────────────────────
FEATURES = [
    {"lookup_key": "advanced_ocr",            "name": "Advanced OCR Engine"},
    {"lookup_key": "priority_support",        "name": "Priority Support"},
    {"lookup_key": "premium_ai",              "name": "Premium AI Models"},
    {"lookup_key": "no_watermark",            "name": "No Watermark"},
    {"lookup_key": "unlimited_translations",  "name": "Unlimited Translations"},
    {"lookup_key": "zero_latency",            "name": "Zero Latency Mode"},
]

# ── Product → Feature Mapping ────────────────────────────────────────
# Update these IDs to match your Stripe products
PRODUCT_FEATURES = {
    # Pro: advanced_ocr + priority_support
    "prod_TvTGHnyQI5aadw": ["advanced_ocr", "priority_support"],
    "prod_TvgAQa0jcFZ1oh": ["advanced_ocr", "priority_support"],  # Pro_QR
    
    # Plus: advanced_ocr + priority_support + premium_ai + no_watermark
    "prod_TvTH3DkRSKXJyK": ["advanced_ocr", "priority_support", "premium_ai", "no_watermark"],
    "prod_TvgA6Trj8aUAu3": ["advanced_ocr", "priority_support", "premium_ai", "no_watermark"],  # Plus_QR
    
    # Ultimate: ALL features
    "prod_TvTIXmFjqqkrxk": ["advanced_ocr", "priority_support", "premium_ai", "no_watermark", "unlimited_translations", "zero_latency"],
    "prod_TvgBQOHUcoUndo": ["advanced_ocr", "priority_support", "premium_ai", "no_watermark", "unlimited_translations", "zero_latency"],  # Ultimate_QR
}


def main():
    print("=" * 60)
    print("  Stripe Feature & Entitlement Setup")
    print("=" * 60)
    
    # 1. Create Features
    created_features = {}
    print("\n📦 Creating Features...")
    for feat in FEATURES:
        try:
            f = stripe.entitlements.Feature.create(
                name=feat["name"],
                lookup_key=feat["lookup_key"],
            )
            created_features[feat["lookup_key"]] = f.id
            print(f"  ✅ {feat['name']} → {f.id}")
        except stripe.error.InvalidRequestError as e:
            if "already exists" in str(e).lower():
                # Feature already exists, retrieve it
                features_list = stripe.entitlements.Feature.list(
                    lookup_key=feat["lookup_key"]
                )
                if features_list.data:
                    created_features[feat["lookup_key"]] = features_list.data[0].id
                    print(f"  ⏭️  {feat['name']} already exists → {features_list.data[0].id}")
                else:
                    print(f"  ❌ {feat['name']} error: {e}")
            else:
                print(f"  ❌ {feat['name']} error: {e}")
    
    # 2. Attach Features to Products
    print("\n🔗 Attaching Features to Products...")
    for product_id, feature_keys in PRODUCT_FEATURES.items():
        # Get product name for display
        try:
            product = stripe.Product.retrieve(product_id)
            product_name = product.name
        except Exception:
            product_name = product_id
        
        print(f"\n  📦 {product_name} ({product_id}):")
        for key in feature_keys:
            if key not in created_features:
                print(f"    ❌ Feature '{key}' not found, skipping")
                continue
            try:
                stripe.Product.create_feature(
                    product_id,
                    entitlement_feature=created_features[key],
                )
                print(f"    ✅ → {key}")
            except stripe.error.InvalidRequestError as e:
                if "already" in str(e).lower():
                    print(f"    ⏭️  → {key} (already attached)")
                else:
                    print(f"    ❌ → {key}: {e}")
    
    print("\n" + "=" * 60)
    print("  ✅ Setup Complete!")
    print("=" * 60)
    print("\nNext: Deploy your webhook handler and create a test subscription.")


if __name__ == "__main__":
    main()

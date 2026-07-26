// @ts-check
export const SITE = {
    title: "Overlayr",
    description: "Game-aware live translation for Windows",
    email: "overlayr.catcode@gmail.com",
    repo: "https://github.com/Overlayr/Overlayr",
    discord: "https://discord.gg/overlayr",
};

export const GITHUB_LATEST_RELEASE = "https://github.com/Overlayr/Overlayr/releases/latest";

export const STRIPE_LINKS = {
    pro: import.meta.env.PUBLIC_STRIPE_PRO_LINK || "",
    pro_qr: import.meta.env.PUBLIC_STRIPE_PRO_PROMPTPAY_LINK || "",
};

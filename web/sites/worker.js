function withIndexPath(request, pathname) {
    const url = new URL(request.url);
    url.pathname = pathname;
    return new Request(url, request);
}

export default {
    async fetch(request, env) {
        if (!env.ASSETS) {
            return new Response("Static asset binding is unavailable.", {
                status: 503,
            });
        }

        const url = new URL(request.url);
        let response = await env.ASSETS.fetch(request);

        if (response.status !== 404 || url.pathname.includes(".")) {
            return response;
        }

        const indexPath = `${url.pathname.replace(/\/$/, "")}/index.html`;
        response = await env.ASSETS.fetch(withIndexPath(request, indexPath));

        if (response.status !== 404) {
            return response;
        }

        return env.ASSETS.fetch(withIndexPath(request, "/404.html"));
    },
};

/* Onboarding snippets for each client harness (spec/agent-auth-plane/12 §4).
 *
 * Data, not code: a template per harness with `{url}` and `{token}` filled in
 * by the Connect panel on the Principal screen and by the token display after
 * a mint. The token is shown once, so the panel's copy carries a placeholder
 * unless it is rendered beside a freshly minted one.
 */
export const CONNECT_SNIPPETS = [
    {
        id: 'claude-code',
        label: 'Claude Code — baseline token',
        text: 'claude mcp add --transport http datum-sync {url} \\\n'
            + '  --header "Authorization: Bearer {token}"',
    },
    {
        id: 'claude-code-oauth',
        label: 'Claude Code — elevated (OAuth), scope pinned',
        text: 'claude mcp add-json datum-sync \\\n'
            + '  \'{"type":"http","url":"{url}","oauth":{"scopes":"mcp:operate"}}\'\n'
            + 'claude mcp login datum-sync --no-browser'
            + '   # open the URL as the sponsor; pick the agent on the consent page',
    },
    {
        id: 'claude-code-helper',
        label: 'Claude Code — rotating token via headersHelper',
        text: '{"mcpServers": {"datum-sync": {"type": "http", "url": "{url}",\n'
            + '  "headersHelper": "cat /etc/datum-sync/headers.json"}}}',
    },
    {
        id: 'codex',
        label: 'Codex — baseline token',
        text: '[mcp_servers.datum-sync]\n'
            + 'url = "{url}"\n'
            + 'bearer_token_env_var = "DATUM_SYNC_TOKEN"   # export DATUM_SYNC_TOKEN={token}',
    },
    {
        id: 'codex-oauth',
        label: 'Codex — elevated (OAuth)',
        text: 'codex mcp add datum-sync --url {url}\n'
            + 'codex mcp login datum-sync       # choose the scope and the agent on the consent page',
    },
    {
        id: 'datum3',
        label: 'Datum-3.0 — baseline token',
        text: '{"mcpServers": {"datum-sync": {"type": "http", "url": "{url}"}}}\n'
            + '# service environment: DATUM_MCP_AUTH_TOKEN={token}  DATUM_MCP_SERVERS=datum-sync',
    },
    {
        id: 'device',
        label: 'Any script — elevate by device code',
        text: 'curl -X POST {url}/../oauth/device -H "Authorization: Bearer {token}" \\\n'
            + '  -d client_id=datum-sync-elevate -d scope=mcp:operate\n'
            + '# a person approves the user_code under Approvals; then poll\n'
            + 'curl -X POST {url}/../oauth/token -d grant_type=urn:ietf:params:oauth:grant-type:device_code \\\n'
            + '  -d client_id=datum-sync-elevate -d device_code=<device_code>',
    },
];

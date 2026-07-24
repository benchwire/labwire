# labwire-mcp

The Labwire MCP adapter: connects to one or more Labwire Instrument Servers
and exposes every declared instrument command as an MCP tool, so Claude and
other MCP clients can drive (simulated) lab hardware natively.

```bash
labwire-mcp ws://127.0.0.1:9520 [ws://... ...]
```

See `examples/mcp-config.json` for a Claude-style MCP server configuration.

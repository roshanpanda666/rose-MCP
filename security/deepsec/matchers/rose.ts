import { regexMatcher, type MatcherPlugin } from "deepsec/config";

// ROSE: every subprocess / adb / shell call built in a tool module or backend is a review candidate.
export const roseSubprocess: MatcherPlugin = {
  slug: "rose-subprocess",
  description: "subprocess/os.system calls in ROSE tools and the phone backend",
  noiseTier: "noisy",
  filePatterns: ["rose/**/*.py", "desktop/*.py", "backend/app/**/*.py"],
  examples: ['subprocess.run(["adb", "shell", cmd])'],
  match(content) {
    return regexMatcher(
      "rose-subprocess",
      [{ regex: /\b(subprocess\.(run|Popen|check_output|call)|os\.system|asyncio\.create_subprocess_\w+)\s*\(/g, label: "process spawn" }],
      content,
    );
  },
};

// ROSE: HTTP entry points (FastMCP custom routes, FastAPI routes, websockets) and file-path tools.
export const roseIngress: MatcherPlugin = {
  slug: "rose-ingress",
  description: "HTTP routes and path-handling tools",
  noiseTier: "noisy",
  filePatterns: ["rose/**/*.py", "backend/app/**/*.py"],
  examples: ['@mcp.custom_route("/x", methods=["POST"])', '@app.get("/api/media/{name}")', "def resolve(path: str):"],
  match(content) {
    return regexMatcher(
      "rose-ingress",
      [
        { regex: /@(mcp\.custom_route|app\.(get|post|put|delete|websocket))\s*\(/g, label: "route" },
        { regex: /def \w*(resolve|safe|allowed)\w*\s*\(/g, label: "path guard" },
      ],
      content,
    );
  },
};

// Android: exported components and intent handlers.
export const roseAndroid: MatcherPlugin = {
  slug: "rose-android",
  description: "Android exported components and intent extras",
  noiseTier: "noisy",
  filePatterns: ["android/**/*.java", "android/**/*.xml"],
  examples: ['android:exported="true"', 'getIntent().getStringExtra("url")'],
  match(content) {
    return regexMatcher(
      "rose-android",
      [
        { regex: /android:exported="true"/g, label: "exported component" },
        { regex: /get\w*Extra\s*\(|getData\s*\(\)/g, label: "intent input" },
      ],
      content,
    );
  },
};

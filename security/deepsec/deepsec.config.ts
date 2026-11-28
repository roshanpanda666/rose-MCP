import { defineConfig, type DeepsecPlugin } from "deepsec/config";
import { generatedMatchersPlugin } from "./generated-matchers.js";
import { roseAndroid, roseIngress, roseSubprocess } from "./matchers/rose.js";

const roseMatchers: DeepsecPlugin = { name: "rose-matchers", matchers: [roseSubprocess, roseIngress, roseAndroid] };

export default defineConfig({
  projects: [
    { id: "rose-MCP", root: ".." },
    { id: "rose-phone-app", root: "../../rose-phone-app" },
    // <deepsec:projects-insert-above>
  ],
  plugins: [generatedMatchersPlugin, roseMatchers],
});

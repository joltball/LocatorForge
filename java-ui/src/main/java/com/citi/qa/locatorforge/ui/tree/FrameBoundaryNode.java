// PHASE: 5.4.1
package com.citi.qa.locatorforge.ui.tree;

import org.json.JSONObject;

/**
 * UI marker for an iframe boundary, rendered with a 🖼 icon prefix. Everything
 * beneath this node lives in a different document, so any generated Selenium
 * locator needs a {@code driver.switchTo().frame(...)} first.
 */
public final class FrameBoundaryNode {

    public final String nodeId;
    public final String selector;
    public final String src;

    public FrameBoundaryNode(String nodeId, String selector, String src) {
        this.nodeId = nodeId;
        this.selector = selector;
        this.src = src;
    }

    public static boolean matches(JSONObject n) {
        return n.optBoolean("is_frame_boundary", false);
    }

    public static FrameBoundaryNode from(JSONObject n) {
        JSONObject attrs = n.optJSONObject("attributes");
        String sel = attrs != null ? attrs.optString("selector", "") : "";
        String src = attrs != null ? attrs.optString("src", "") : "";
        if (sel.isBlank()) sel = n.optString("name", "iframe");
        return new FrameBoundaryNode(n.optString("node_id"), sel, src);
    }

    @Override public String toString() {
        String host = src;
        int i = host.indexOf("://");
        if (i >= 0) {
            int j = host.indexOf('/', i + 3);
            host = j > 0 ? host.substring(i + 3, j) : host.substring(i + 3);
        }
        String suffix = host.isBlank() ? "" : "  (" + host + ")";
        return "🖼 " + selector + " [iframe]" + suffix;
    }
}

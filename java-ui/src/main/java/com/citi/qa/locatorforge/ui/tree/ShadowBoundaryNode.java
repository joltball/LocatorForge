// PHASE: 3.4.3
package com.citi.qa.locatorforge.ui.tree;

import org.json.JSONObject;

/** UI marker for a shadow-root boundary node, rendered with a ⚡ icon prefix. */
public final class ShadowBoundaryNode {

    public final String nodeId;
    public final String hostSelector;
    public final String shadowType;

    public ShadowBoundaryNode(String nodeId, String hostSelector, String shadowType) {
        this.nodeId = nodeId;
        this.hostSelector = hostSelector;
        this.shadowType = shadowType;
    }

    public static boolean matches(JSONObject n) {
        return "_shadow_boundary".equals(n.optString("role"));
    }

    public static ShadowBoundaryNode from(JSONObject n) {
        JSONObject attrs = n.optJSONObject("attributes");
        String sel = attrs != null ? attrs.optString("host_selector", "?") : "?";
        String type = attrs != null ? attrs.optString("shadow_type", "open") : "open";
        return new ShadowBoundaryNode(n.optString("node_id"), sel, type);
    }

    @Override public String toString() {
        return "⚡ " + hostSelector + " [shadow-root: " + shadowType + "]";
    }
}

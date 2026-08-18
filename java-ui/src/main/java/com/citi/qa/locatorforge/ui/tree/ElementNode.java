// PHASE: 1.3.3 (+ 5.1 page indicator / recorded elements)
package com.citi.qa.locatorforge.ui.tree;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * UI-side projection of a backend TreeNode. Holds enough to drive the JTree
 * label and the "Push to POM" payload.
 *
 * <p>Every node carries a {@code page} indicator (e.g. {@code CheckoutPayment}),
 * derived backend-side from the URL it was captured on, so a session spanning
 * several pages can be split into one POM class per page.
 */
public final class ElementNode {

    public final String nodeId;
    public final String role;
    public final String name;
    public final String elementType;
    public final String chosenStrategy;
    public final String chosenValue;
    public final String page;
    public final String pageUrl;
    public final JSONObject raw;

    /** Set for recorded elements: the backend already ranked these. */
    private final String annotationOverride;
    /** Recorded-only decoration: which actions the user performed, and how often. */
    private final String actionSummary;
    private final Boolean unique;

    private ElementNode(String nodeId, String role, String name, String elementType,
                        String chosenStrategy, String chosenValue, String page, String pageUrl,
                        String annotationOverride, String actionSummary, Boolean unique,
                        JSONObject raw) {
        this.nodeId = nodeId;
        this.role = role;
        this.name = name;
        this.elementType = elementType;
        this.chosenStrategy = chosenStrategy;
        this.chosenValue = chosenValue;
        this.page = page;
        this.pageUrl = pageUrl;
        this.annotationOverride = annotationOverride;
        this.actionSummary = actionSummary;
        this.unique = unique;
        this.raw = raw;
    }

    public static ElementNode from(JSONObject n) {
        return from(n, null, null);
    }

    /**
     * @param inheritedPage page stamped on the tree root; children inherit it so
     *                      every locator in the tree shows where it came from.
     */
    public static ElementNode from(JSONObject n, String inheritedPage, String inheritedUrl) {
        String nodeId = n.optString("node_id", "?");
        String role = n.optString("role", "unknown");
        String name = n.optString("name", "");
        String et = n.optString("element_type", "interactive");
        String page = n.optString("page", null);
        String pageUrl = n.optString("page_url", null);
        if (page == null || page.isBlank()) page = inheritedPage;
        if (pageUrl == null || pageUrl.isBlank()) pageUrl = inheritedUrl;

        JSONObject attrs = n.optJSONObject("attributes");
        String strategy = "css";
        String value = role;
        if (attrs != null) {
            if (attrs.has("data-testid")) {
                strategy = "data-testid"; value = attrs.getString("data-testid");
            } else if (attrs.has("id")) {
                strategy = "id"; value = attrs.getString("id");
            } else if (attrs.has("aria-label")) {
                strategy = "aria-label"; value = attrs.getString("aria-label");
            } else if (attrs.has("name")) {
                strategy = "name"; value = attrs.getString("name");
            } else if (name != null && !name.isBlank()) {
                strategy = "name-text"; value = name;
            }
        }
        return new ElementNode(nodeId, role, name, et, strategy, value, page, pageUrl,
                null, null, null, n);
    }

    /** Build from one entry of {@code GET /record} / the {@code element_recorded} event. */
    public static ElementNode fromRecorded(JSONObject el) {
        JSONObject best = el.optJSONObject("best");
        String strategy = best == null ? "css" : best.optString("strategy", "css");
        String value = best == null ? el.optString("tag", "") : best.optString("value", "");
        String annotation = best == null ? null : best.optString("selenium", null);
        Boolean uniq = null;
        if (best != null && best.has("is_unique") && !best.isNull("is_unique")) {
            uniq = best.optBoolean("is_unique");
        }

        StringBuilder actions = new StringBuilder();
        JSONArray arr = el.optJSONArray("actions");
        for (int i = 0; arr != null && i < arr.length(); i++) {
            if (i > 0) actions.append('+');
            actions.append(arr.optString(i));
        }
        int hits = el.optInt("hits", 1);
        if (hits > 1) actions.append(" ×").append(hits);

        return new ElementNode(
                "rec:" + strategy + "=" + value,
                el.optString("role", el.optString("tag", "unknown")),
                el.optString("name", ""),
                el.optString("element_type", "interactive"),
                strategy, value,
                el.optString("page", null), el.optString("page_url", null),
                annotation, actions.toString(), uniq, el);
    }

    public boolean isRecorded() {
        return nodeId != null && nodeId.startsWith("rec:");
    }

    /** Render as Selenium {@code @FindBy(...)} hint per ADR-04 default. */
    public String findByAnnotation() {
        if (annotationOverride != null && !annotationOverride.isBlank()) return annotationOverride;
        return switch (chosenStrategy) {
            case "id"           -> "@FindBy(id = \"" + chosenValue + "\")";
            case "name"         -> "@FindBy(name = \"" + chosenValue + "\")";
            case "data-testid"  -> "@FindBy(css = \"[data-testid='" + chosenValue + "']\")";
            case "aria-label"   -> "@FindBy(css = \"[aria-label='" + chosenValue + "']\")";
            default             -> "@FindBy(css = \"" + role + "\")"; // best-effort placeholder
        };
    }

    @Override public String toString() {
        StringBuilder sb = new StringBuilder();
        if (page != null && !page.isBlank()) sb.append('[').append(page).append("] ");
        sb.append(role);
        if (name != null && !name.isBlank()) sb.append(": ").append(name);
        if (actionSummary != null && !actionSummary.isBlank()) {
            sb.append("  (").append(actionSummary).append(')');
        }
        sb.append("   ").append(findByAnnotation());
        if (unique != null) sb.append(unique ? "  ✓unique" : "  ⚠not unique");
        return sb.toString();
    }
}

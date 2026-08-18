// PHASE: 5.1
package com.citi.qa.locatorforge.ui.tree;

import org.json.JSONArray;
import org.json.JSONObject;

import javax.swing.*;
import javax.swing.tree.DefaultMutableTreeNode;
import javax.swing.tree.DefaultTreeModel;
import javax.swing.tree.TreePath;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Elements captured by the interaction recorder, grouped under the page they
 * were touched on. Fed incrementally by the {@code element_recorded} WebSocket
 * event so the tree fills in as the user drives the browser.
 */
public final class RecordingPanel extends JTree {

    /** page name -> (dedupe key -> element json), both in insertion order. */
    private final Map<String, Map<String, JSONObject>> byPage = new LinkedHashMap<>();
    private final DefaultMutableTreeNode root = new DefaultMutableTreeNode("Not recording");
    private final DefaultTreeModel model = new DefaultTreeModel(root);

    public RecordingPanel() {
        setModel(model);
        setRootVisible(true);
        setShowsRootHandles(true);
    }

    /** Replace everything from a {@code GET /record} snapshot. */
    public void applySnapshot(JSONObject snapshot) {
        byPage.clear();
        JSONArray pages = snapshot == null ? null : snapshot.optJSONArray("pages");
        for (int i = 0; pages != null && i < pages.length(); i++) {
            JSONObject page = pages.getJSONObject(i);
            JSONArray els = page.optJSONArray("elements");
            for (int j = 0; els != null && j < els.length(); j++) {
                put(els.getJSONObject(j));
            }
        }
        rebuild();
    }

    /** Merge one element from the {@code element_recorded} event. */
    public void addElement(JSONObject element) {
        put(element);
        rebuild();
    }

    public void clear() {
        byPage.clear();
        rebuild();
    }

    public int count() {
        int n = 0;
        for (Map<String, JSONObject> m : byPage.values()) n += m.size();
        return n;
    }

    /** Selected recorded elements; a page node selects everything beneath it. */
    public List<ElementNode> getSelectedElements() {
        List<ElementNode> out = new ArrayList<>();
        TreePath[] paths = getSelectionPaths();
        if (paths == null) return out;
        for (TreePath path : paths) {
            Object last = path.getLastPathComponent();
            if (!(last instanceof DefaultMutableTreeNode dn)) continue;
            if (dn.getUserObject() instanceof ElementNode en) {
                out.add(en);
            } else {
                for (int i = 0; i < dn.getChildCount(); i++) {
                    Object child = ((DefaultMutableTreeNode) dn.getChildAt(i)).getUserObject();
                    if (child instanceof ElementNode en2 && !out.contains(en2)) out.add(en2);
                }
            }
        }
        return out;
    }

    private void put(JSONObject element) {
        String page = element.optString("page", "Unknown");
        JSONObject best = element.optJSONObject("best");
        String key = best == null
                ? element.optString("tag", "?") + ":" + element.optString("name", "")
                : best.optString("strategy", "?") + "=" + best.optString("value", "");
        byPage.computeIfAbsent(page, p -> new LinkedHashMap<>()).put(key, element);
    }

    private void rebuild() {
        int total = count();
        DefaultMutableTreeNode newRoot = new DefaultMutableTreeNode(
                total == 0 ? "No elements recorded yet" : "Recorded (" + total + ")");
        for (Map.Entry<String, Map<String, JSONObject>> e : byPage.entrySet()) {
            DefaultMutableTreeNode pageNode =
                    new DefaultMutableTreeNode(e.getKey() + "  (" + e.getValue().size() + ")");
            for (JSONObject el : e.getValue().values()) {
                pageNode.add(new DefaultMutableTreeNode(ElementNode.fromRecorded(el)));
            }
            newRoot.add(pageNode);
        }
        model.setRoot(newRoot);
        model.reload();
        for (int i = 0; i < getRowCount(); i++) expandRow(i);
    }
}

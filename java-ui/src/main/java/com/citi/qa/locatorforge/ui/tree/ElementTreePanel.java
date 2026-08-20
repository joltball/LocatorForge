// PHASE: 1.3.3
package com.citi.qa.locatorforge.ui.tree;

import com.citi.qa.locatorforge.ui.api.ApiClient;
import org.json.JSONArray;
import org.json.JSONObject;

import javax.swing.*;
import javax.swing.tree.DefaultMutableTreeNode;
import javax.swing.tree.DefaultTreeModel;
import java.util.ArrayList;
import java.util.Enumeration;
import java.util.List;
import java.util.Locale;

/**
 * Displays the live element tree returned from {@code GET /tree}. Each node is
 * rendered as "{role}: {name}" with a Selenium {@code @FindBy(...)} hint, taken
 * from the most discriminating attribute available at this phase (data-testid /
 * id / aria-label).
 */
public final class ElementTreePanel extends JTree {

    private final ApiClient api;
    private final DefaultMutableTreeNode root = new DefaultMutableTreeNode("Loading…");
    private final DefaultTreeModel model = new DefaultTreeModel(root);

    public ElementTreePanel(ApiClient api) {
        super();
        this.api = api;
        setModel(model);
        setRootVisible(true);
        setShowsRootHandles(true);
    }

    /** Returns the snapshot of currently selected element nodes. */
    public List<ElementNode> getSelectedElements() {
        List<ElementNode> out = new ArrayList<>();
        var paths = getSelectionPaths();
        if (paths == null) return out;
        for (var path : paths) {
            Object last = path.getLastPathComponent();
            if (last instanceof DefaultMutableTreeNode dn && dn.getUserObject() instanceof ElementNode en) {
                out.add(en);
            }
        }
        return out;
    }

    /** Cheap client-side filter: expand matching paths and select the first hit. */
    public void applyFilter(String query) {
        if (query == null || query.isBlank()) return;
        String q = query.toLowerCase(Locale.ROOT);
        Object rootObj = model.getRoot();
        if (!(rootObj instanceof DefaultMutableTreeNode r)) return;
        Enumeration<?> all = r.preorderEnumeration();
        while (all.hasMoreElements()) {
            Object next = all.nextElement();
            if (next instanceof DefaultMutableTreeNode dn) {
                Object uo = dn.getUserObject();
                if (uo == null) continue;
                if (uo.toString().toLowerCase(Locale.ROOT).contains(q)) {
                    var path = new javax.swing.tree.TreePath(dn.getPath());
                    expandPath(path);
                    setSelectionPath(path);
                    scrollPathToVisible(path);
                    return;
                }
            }
        }
    }

    public void refresh() {
        final long t0 = System.currentTimeMillis();
        System.out.println("[ui] refresh(): starting GET /tree");
        model.setRoot(new DefaultMutableTreeNode("Loading…"));
        model.reload();
        SwingWorker<JSONObject, Void> worker = new SwingWorker<>() {
            @Override protected JSONObject doInBackground() throws Exception {
                return api.getTree();
            }

            @Override protected void done() {
                long elapsed = System.currentTimeMillis() - t0;
                try {
                    JSONObject resp = get();
                    int rawLen = resp == null ? 0 : resp.toString().length();
                    Object treeField = resp == null ? null : resp.opt("tree");
                    System.out.println("[ui] refresh(): /tree returned in " + elapsed
                            + "ms, raw_json=" + rawLen + " bytes, tree="
                            + (treeField == null ? "null" :
                                 (treeField == JSONObject.NULL ? "JSONObject.NULL" : treeField.getClass().getSimpleName())));
                    if (treeField instanceof JSONObject treeRoot) {
                        System.out.println("[ui] refresh(): root role=" + treeRoot.optString("role")
                                + " name=" + treeRoot.optString("name")
                                + " direct_children=" + (treeRoot.optJSONArray("children") == null ? 0 : treeRoot.optJSONArray("children").length()));
                        DefaultMutableTreeNode newRoot = buildNode(
                                treeRoot,
                                treeRoot.optString("page", null),
                                treeRoot.optString("page_url", null));
                        System.out.println("[ui] refresh(): built " + countNodes(newRoot) + " Swing tree nodes");
                        model.setRoot(newRoot);
                        model.reload();
                        for (int i = 0; i < getRowCount(); i++) expandRow(i);
                        System.out.println("[ui] refresh(): render complete, visible rows=" + getRowCount());
                    } else {
                        System.out.println("[ui] refresh(): tree field absent or null — showing placeholder");
                        model.setRoot(new DefaultMutableTreeNode("(no page target — open a tab in Chrome)"));
                        model.reload();
                    }
                } catch (Exception e) {
                    System.out.println("[ui] refresh(): EXCEPTION after " + elapsed + "ms: " + e);
                    e.printStackTrace();
                    model.setRoot(new DefaultMutableTreeNode("Error: " + e.getMessage()));
                    model.reload();
                }
            }
        };
        worker.execute();
    }

    private static int countNodes(DefaultMutableTreeNode n) {
        int c = 1;
        for (int i = 0; i < n.getChildCount(); i++) {
            c += countNodes((DefaultMutableTreeNode) n.getChildAt(i));
        }
        return c;
    }

    /** Page identity is stamped on the root only; children inherit it. */
    private static DefaultMutableTreeNode buildNode(JSONObject n, String page, String pageUrl) {
        Object payload;
        if (ShadowBoundaryNode.matches(n)) {
            payload = ShadowBoundaryNode.from(n);
        } else if (FrameBoundaryNode.matches(n)) {
            payload = FrameBoundaryNode.from(n);
        } else {
            payload = ElementNode.from(n, page, pageUrl);
        }
        DefaultMutableTreeNode dn = new DefaultMutableTreeNode(payload);
        JSONArray children = n.optJSONArray("children");
        if (children != null) {
            for (int i = 0; i < children.length(); i++) {
                dn.add(buildNode(children.getJSONObject(i), page, pageUrl));
            }
        }
        return dn;
    }
}

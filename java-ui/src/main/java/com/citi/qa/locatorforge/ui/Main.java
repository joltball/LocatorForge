// PHASE: 1.3.1 + 2.2.x
package com.citi.qa.locatorforge.ui;

import com.citi.qa.locatorforge.ui.api.ApiClient;
import com.citi.qa.locatorforge.ui.api.LocatorClient;
import com.citi.qa.locatorforge.ui.api.WsClient;
import com.citi.qa.locatorforge.ui.editor.LocatorEditorPanel;
import com.citi.qa.locatorforge.ui.toolbar.MainToolBar;
import com.citi.qa.locatorforge.ui.tree.ElementNode;
import com.citi.qa.locatorforge.ui.tree.ElementTreePanel;
import com.citi.qa.locatorforge.ui.tree.RecordingPanel;
import com.formdev.flatlaf.FlatLightLaf;
import org.json.JSONObject;

import javax.swing.*;
import javax.swing.event.DocumentEvent;
import javax.swing.event.DocumentListener;
import javax.swing.event.TreeSelectionListener;
import javax.swing.tree.DefaultMutableTreeNode;
import java.awt.*;

public final class Main {

    public static void main(String[] args) {
        int apiPort = parseArgInt(args, "--api-port=", 8765);
        FlatLightLaf.setup();

        SwingUtilities.invokeLater(() -> {
            ApiClient api = new ApiClient("http://127.0.0.1:" + apiPort);
            LocatorClient lc = new LocatorClient(api);

            ElementTreePanel tree = new ElementTreePanel(api);
            RecordingPanel recorded = new RecordingPanel();
            LocatorEditorPanel editor = new LocatorEditorPanel(lc);

            JTabbedPane leftTabs = new JTabbedPane();
            // Push acts on whichever tab is in front, so the two trees never
            // fight over the selection.
            MainToolBar toolBar = new MainToolBar(api, lc, tree, editor,
                    () -> leftTabs.getSelectedIndex() == 1
                            ? recorded.getSelectedElements()
                            : tree.getSelectedElements());

            // Tree selection → load locators on right pane + highlight in browser
            tree.addTreeSelectionListener((TreeSelectionListener) e -> {
                Object last = tree.getLastSelectedPathComponent();
                if (last instanceof DefaultMutableTreeNode dn && dn.getUserObject() instanceof ElementNode en) {
                    editor.loadNode(en.nodeId);
                    new Thread(() -> {
                        try { lc.highlight(en.nodeId); } catch (Exception ignored) {}
                    }, "lf-highlight").start();
                }
            });

            // Search/filter bar
            JTextField filter = new JTextField();
            filter.setToolTipText("Filter by role / name / locator");
            filter.getDocument().addDocumentListener(new DocumentListener() {
                @Override public void insertUpdate(DocumentEvent e) { tree.applyFilter(filter.getText()); }
                @Override public void removeUpdate(DocumentEvent e) { tree.applyFilter(filter.getText()); }
                @Override public void changedUpdate(DocumentEvent e) { tree.applyFilter(filter.getText()); }
            });

            JPanel left = new JPanel(new BorderLayout(4, 4));
            JPanel topLeft = new JPanel(new BorderLayout(4, 4));
            topLeft.setBorder(BorderFactory.createEmptyBorder(4, 4, 4, 4));
            topLeft.add(new JLabel("Filter:"), BorderLayout.WEST);
            topLeft.add(filter, BorderLayout.CENTER);
            left.add(topLeft, BorderLayout.NORTH);
            leftTabs.addTab("Elements", new JScrollPane(tree));
            leftTabs.addTab("Recorded", new JScrollPane(recorded));
            left.add(leftTabs, BorderLayout.CENTER);

            JSplitPane split = new JSplitPane(JSplitPane.HORIZONTAL_SPLIT, left, editor);
            split.setDividerLocation(560);

            JFrame frame = new JFrame("LocatorForge");
            frame.setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
            frame.setLayout(new BorderLayout());
            frame.add(toolBar, BorderLayout.NORTH);
            frame.add(split, BorderLayout.CENTER);

            JLabel status = new JLabel("  Connected to API on port " + apiPort);
            status.setBorder(BorderFactory.createEmptyBorder(4, 8, 4, 8));
            frame.add(status, BorderLayout.SOUTH);

            // WebSocket events from backend
            WsClient ws = new WsClient("ws://127.0.0.1:" + apiPort + "/ws", evt -> {
                String event = evt.optString("event", "");
                JSONObject data = evt.optJSONObject("data");
                SwingUtilities.invokeLater(() -> {
                    switch (event) {
                        case "page_navigated" -> status.setText("  Page: " + (data == null ? "?" : data.optString("url")));
                        case "tree_updated"   -> tree.refresh();
                        case "element_picked" -> {
                            status.setText("  Element picked (verification) — appended to tree");
                            tree.refresh();
                        }
                        case "recording_state" -> {
                            boolean on = data != null && data.optBoolean("recording");
                            toolBar.setRecordingState(on);
                            leftTabs.setSelectedIndex(on ? 1 : leftTabs.getSelectedIndex());
                            status.setText(on
                                    ? "  ● Recording — use the app in Chrome; every element you touch is captured"
                                    : "  Recording stopped — " + (data == null ? 0 : data.optInt("count"))
                                      + " element(s) in .locatorforge/recording.json");
                        }
                        case "element_recorded" -> {
                            if (data != null) {
                                JSONObject el = data.optJSONObject("element");
                                if (el != null) recorded.addElement(el);
                                int n = data.optInt("count");
                                toolBar.setRecordedCount(n);
                                leftTabs.setTitleAt(1, "Recorded (" + n + ")");
                                status.setText("  ● Recording — captured [" + (el == null ? "?" : el.optString("page"))
                                        + "] " + (el == null ? "" : el.optString("role") + ": " + el.optString("name")));
                            }
                        }
                        case "recording_cleared" -> {
                            recorded.clear();
                            toolBar.setRecordedCount(0);
                            leftTabs.setTitleAt(1, "Recorded");
                        }
                        case "browser_disconnected" -> status.setText("  Browser disconnected — reconnecting…");
                        case "browser_reconnected"  -> status.setText("  Browser reconnected");
                        default -> {}
                    }
                });
            });
            ws.start();
            frame.addWindowListener(new java.awt.event.WindowAdapter() {
                @Override public void windowClosing(java.awt.event.WindowEvent e) { ws.stop(); }
            });

            frame.setSize(1200, 760);
            frame.setLocationRelativeTo(null);
            frame.setVisible(true);

            tree.refresh();

            // The backend may already be recording (UI restarted mid-session) —
            // adopt its state rather than assuming idle.
            new Thread(() -> {
                try {
                    JSONObject snap = lc.getRecording();
                    SwingUtilities.invokeLater(() -> {
                        recorded.applySnapshot(snap);
                        toolBar.setRecordingState(snap.optBoolean("recording"));
                        toolBar.setRecordedCount(snap.optInt("count"));
                        if (snap.optInt("count") > 0) {
                            leftTabs.setTitleAt(1, "Recorded (" + snap.optInt("count") + ")");
                        }
                    });
                } catch (Exception ignored) {
                    // Backend predates /record, or isn't up yet; toolbar stays idle.
                }
            }, "lf-record-sync").start();
        });
    }

    private static int parseArgInt(String[] args, String prefix, int defaultValue) {
        for (String a : args) {
            if (a.startsWith(prefix)) {
                try { return Integer.parseInt(a.substring(prefix.length())); }
                catch (NumberFormatException ignored) {}
            }
        }
        return defaultValue;
    }
}

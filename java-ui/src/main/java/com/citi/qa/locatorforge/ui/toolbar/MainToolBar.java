// PHASE: 1.3.4 + 2.2.3
package com.citi.qa.locatorforge.ui.toolbar;

import com.citi.qa.locatorforge.ui.api.ApiClient;
import com.citi.qa.locatorforge.ui.api.LocatorClient;
import com.citi.qa.locatorforge.ui.editor.LocatorEditorPanel;
import com.citi.qa.locatorforge.ui.tree.ElementNode;
import com.citi.qa.locatorforge.ui.tree.ElementTreePanel;
import org.json.JSONArray;
import org.json.JSONObject;

import javax.swing.*;
import java.awt.Color;
import java.util.List;
import java.util.function.Supplier;

public final class MainToolBar extends JToolBar {

    private static final Color REC_ON = new Color(0xD0, 0x21, 0x29);
    private static final int BLINK_MS = 500;

    private final ApiClient api;
    private final LocatorClient lc;
    private final ElementTreePanel tree;
    private final LocatorEditorPanel editor;
    private final Supplier<List<ElementNode>> selectionSupplier;

    private final JButton record = new JButton("● Record");
    private final JButton stop = new JButton("■ Stop");
    private final Color recordIdleFg;
    private final Timer blinker;
    private boolean blinkOn = false;
    private boolean recording = false;
    private int recordedCount = 0;

    public MainToolBar(ApiClient api, LocatorClient lc, ElementTreePanel tree,
                       LocatorEditorPanel editor, Supplier<List<ElementNode>> selectionSupplier) {
        super();
        this.api = api;
        this.lc = lc;
        this.tree = tree;
        this.editor = editor;
        this.selectionSupplier = selectionSupplier;
        setFloatable(false);

        JButton refresh = new JButton("Refresh");
        refresh.addActionListener(e -> {
            System.out.println("[ui] Refresh clicked");
            tree.refresh();
        });
        add(refresh);

        JButton add = new JButton("Add Element");
        add.setToolTipText("Activate inspect mode in Chrome — click a verification element");
        add.addActionListener(e -> onAddElement());
        add(add);

        addSeparator();

        recordIdleFg = record.getForeground();
        record.setToolTipText("Capture every element you click or type into, while you use the app normally");
        record.addActionListener(e -> onRecord());
        add(record);

        stop.setToolTipText("Stop recording and write .locatorforge/recording.json");
        stop.setEnabled(false);
        stop.addActionListener(e -> onStop());
        add(stop);

        // Drives the Record button's blink. Started on record, stopped on stop —
        // never left running, or the button keeps flashing after the session ends.
        blinker = new Timer(BLINK_MS, e -> {
            blinkOn = !blinkOn;
            record.setForeground(blinkOn ? REC_ON : recordIdleFg);
            record.setText((blinkOn ? "● " : "○ ") + recordingLabel());
        });

        addSeparator();

        JButton push = new JButton("Push to POM");
        push.addActionListener(e -> onPush());
        add(push);
    }

    // ---- recording ------------------------------------------------------

    private String recordingLabel() {
        return recordedCount > 0 ? "Recording (" + recordedCount + ")" : "Recording";
    }

    /**
     * Single source of truth for the button states. Driven both by the local
     * click and by the backend's {@code recording_state} WebSocket event, so
     * the UI stays correct if recording is started or stopped elsewhere.
     */
    public void setRecordingState(boolean isRecording) {
        this.recording = isRecording;
        record.setEnabled(!isRecording);
        stop.setEnabled(isRecording);
        if (isRecording) {
            if (!blinker.isRunning()) {
                blinkOn = true;
                blinker.start();
            }
            record.setForeground(REC_ON);
            record.setText("● " + recordingLabel());
        } else {
            blinker.stop();
            record.setForeground(recordIdleFg);
            record.setText("● Record");
        }
    }

    /** Live capture count, shown in the blinking label. */
    public void setRecordedCount(int count) {
        this.recordedCount = count;
        if (recording) record.setText((blinkOn ? "● " : "○ ") + recordingLabel());
    }

    public boolean isRecording() {
        return recording;
    }

    private void onRecord() {
        recordedCount = 0;                  // before the label is built, not after
        setRecordingState(true);            // optimistic; reverted if the call fails
        SwingWorker<Void, Void> w = new SwingWorker<>() {
            @Override protected Void doInBackground() throws Exception {
                lc.startRecording(true);
                return null;
            }
            @Override protected void done() {
                try {
                    get();
                } catch (Exception ex) {
                    setRecordingState(false);
                    JOptionPane.showMessageDialog(MainToolBar.this,
                            "Could not start recording: " + ex.getMessage(),
                            "Record", JOptionPane.ERROR_MESSAGE);
                }
            }
        };
        w.execute();
    }

    private void onStop() {
        setRecordingState(false);
        SwingWorker<org.json.JSONObject, Void> w = new SwingWorker<>() {
            @Override protected org.json.JSONObject doInBackground() throws Exception {
                return lc.stopRecording();
            }
            @Override protected void done() {
                try {
                    org.json.JSONObject resp = get();
                    JOptionPane.showMessageDialog(MainToolBar.this,
                            "Recorded " + resp.optInt("count") + " element(s) across "
                                    + (resp.optJSONArray("pages") == null ? 0 : resp.optJSONArray("pages").length())
                                    + " page(s)\nWritten to " + resp.optString("written"),
                            "Recording stopped", JOptionPane.INFORMATION_MESSAGE);
                } catch (Exception ex) {
                    JOptionPane.showMessageDialog(MainToolBar.this,
                            "Stop failed: " + ex.getMessage(),
                            "Record", JOptionPane.ERROR_MESSAGE);
                }
            }
        };
        w.execute();
    }

    private void onAddElement() {
        SwingWorker<Void, Void> w = new SwingWorker<>() {
            @Override protected Void doInBackground() throws Exception { lc.pickElement(); return null; }
            @Override protected void done() {
                try { get(); } catch (Exception ex) {
                    JOptionPane.showMessageDialog(MainToolBar.this,
                            "Element picker failed: " + ex.getMessage(),
                            "Add Element", JOptionPane.ERROR_MESSAGE);
                }
            }
        };
        w.execute();
    }

    private void onPush() {
        List<ElementNode> selected = selectionSupplier.get();
        if (selected.isEmpty()) {
            JOptionPane.showMessageDialog(this,
                    "Select one or more tree nodes first.", "Push to POM",
                    JOptionPane.WARNING_MESSAGE);
            return;
        }
        // Elements from different pages belong in different POM classes; pushing
        // them into one file silently would be the wrong default.
        List<String> pages = selected.stream()
                .map(n -> n.page == null || n.page.isBlank() ? "?" : n.page)
                .distinct().toList();
        if (pages.size() > 1) {
            int choice = JOptionPane.showConfirmDialog(this,
                    "Selection spans " + pages.size() + " pages: " + String.join(", ", pages)
                            + "\n\nPush them all into a single POM file anyway?",
                    "Push to POM", JOptionPane.YES_NO_OPTION, JOptionPane.WARNING_MESSAGE);
            if (choice != JOptionPane.YES_OPTION) return;
        }
        String suggested = "src/test/java/pages/"
                + (pages.size() == 1 && !"?".equals(pages.get(0)) ? pages.get(0) : "Untitled")
                + "Page.java";
        String pomPath = JOptionPane.showInputDialog(this,
                "POM file path (relative to repo root):", suggested);
        if (pomPath == null || pomPath.isBlank()) return;

        JSONArray mods = new JSONArray();
        // If exactly one element selected and editor has a chosen candidate, use it.
        JSONObject chosen = selected.size() == 1 ? editor.getChosenCandidate() : null;
        for (ElementNode n : selected) {
            JSONObject mod = new JSONObject();
            mod.put("action", "update");
            mod.put("element_name", deriveName(n));
            mod.put("locator_format", "selenium");
            if (n.page != null && !n.page.isBlank()) mod.put("page", n.page);
            if (n.pageUrl != null && !n.pageUrl.isBlank()) mod.put("page_url", n.pageUrl);

            JSONObject newLoc = new JSONObject();
            String strategy = n.chosenStrategy;
            String value = n.chosenValue;
            String annotation = n.findByAnnotation();
            if (chosen != null && selected.size() == 1) {
                strategy = chosen.optString("strategy", strategy);
                value = chosen.optString("value", value);
                annotation = chosen.optString("selenium", annotation);
            }
            newLoc.put("strategy", strategy);
            newLoc.put("value", value);
            mod.put("new_locator", newLoc);
            mod.put("annotation_format", annotation);
            mod.put("shadow_chain", new JSONArray());
            mod.put("element_type", "verification".equals(n.elementType) ? "verification" : "interactive");
            mod.put("access_modifier", "private");
            mods.put(mod);
        }

        SwingWorker<JSONObject, Void> worker = new SwingWorker<>() {
            @Override protected JSONObject doInBackground() throws Exception {
                return api.push(pomPath, "selenium-java", mods);
            }

            @Override protected void done() {
                try {
                    JSONObject resp = get();
                    JOptionPane.showMessageDialog(MainToolBar.this,
                            "Wrote " + resp.optInt("count") + " modification(s)\nto " + resp.optString("written"),
                            "Push to POM", JOptionPane.INFORMATION_MESSAGE);
                } catch (Exception ex) {
                    JOptionPane.showMessageDialog(MainToolBar.this,
                            "Push failed: " + ex.getMessage(),
                            "Push to POM", JOptionPane.ERROR_MESSAGE);
                }
            }
        };
        worker.execute();
    }

    private static String deriveName(ElementNode n) {
        String base = (n.name != null && !n.name.isBlank()) ? n.name : n.chosenValue;
        if (base == null || base.isBlank()) base = n.role;
        String[] parts = base.replaceAll("[^A-Za-z0-9 ]", " ").trim().split("\\s+");
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < parts.length; i++) {
            String p = parts[i];
            if (p.isEmpty()) continue;
            if (i == 0) sb.append(Character.toLowerCase(p.charAt(0))).append(p.substring(1));
            else sb.append(Character.toUpperCase(p.charAt(0))).append(p.substring(1));
        }
        return sb.length() == 0 ? "element" : sb.toString();
    }
}

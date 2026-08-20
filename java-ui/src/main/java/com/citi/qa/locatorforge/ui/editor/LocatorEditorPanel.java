// PHASE: 2.2.1 + 2.2.2
package com.citi.qa.locatorforge.ui.editor;

import com.citi.qa.locatorforge.ui.api.LocatorClient;
import org.json.JSONArray;
import org.json.JSONObject;

import javax.swing.*;
import javax.swing.event.DocumentEvent;
import javax.swing.event.DocumentListener;
import javax.swing.table.AbstractTableModel;
import javax.swing.table.TableCellRenderer;
import java.awt.*;
import java.util.HashSet;
import java.util.Set;
import javax.swing.Timer;

/**
 * Shows every candidate locator for a selected tree node, with a uniqueness
 * badge. Double-clicking a row toggles its Selenium↔Playwright display.
 *
 * Also hosts the custom-locator field with live validation against the running
 * page (POST /validate).
 */
public final class LocatorEditorPanel extends JPanel {

    private final LocatorClient client;
    private final CandidateTable model = new CandidateTable();
    private final JTable table;
    private final JTextField customField = new JTextField();
    private final JComboBox<String> strategyBox = new JComboBox<>(new String[]{
            "css", "xpath", "id", "data-testid", "aria-label", "name"
    });
    private final JLabel validateBadge = new JLabel(" ");
    private final Set<Integer> playwrightRows = new HashSet<>();
    private final Timer validateDebounce;
    private String currentNodeId;

    public LocatorEditorPanel(LocatorClient client) {
        super(new BorderLayout());
        this.client = client;
        this.table = new JTable(model) {
            @Override public String getToolTipText(java.awt.event.MouseEvent e) {
                int row = rowAtPoint(e.getPoint());
                String t = model.tooltipFor(row);
                return t != null ? t : super.getToolTipText(e);
            }
        };
        table.getColumnModel().getColumn(0).setCellRenderer(new BadgeRenderer());
        table.setRowHeight(22);
        table.addMouseListener(new java.awt.event.MouseAdapter() {
            @Override public void mouseClicked(java.awt.event.MouseEvent e) {
                int row = table.rowAtPoint(e.getPoint());
                if (row < 0) return;
                if (e.getClickCount() == 2) {
                    if (playwrightRows.contains(row)) playwrightRows.remove(row);
                    else playwrightRows.add(row);
                    model.fireTableRowsUpdated(row, row);
                }
            }
        });

        add(new JScrollPane(table), BorderLayout.CENTER);

        JPanel south = new JPanel(new BorderLayout(4, 4));
        south.setBorder(BorderFactory.createTitledBorder("Custom locator"));
        JPanel input = new JPanel(new BorderLayout(4, 4));
        input.add(strategyBox, BorderLayout.WEST);
        input.add(customField, BorderLayout.CENTER);
        input.add(validateBadge, BorderLayout.EAST);
        south.add(input, BorderLayout.CENTER);
        add(south, BorderLayout.SOUTH);

        validateDebounce = new Timer(300, e -> runValidate());
        validateDebounce.setRepeats(false);

        customField.getDocument().addDocumentListener(new DocumentListener() {
            @Override public void insertUpdate(DocumentEvent e) { validateDebounce.restart(); }
            @Override public void removeUpdate(DocumentEvent e) { validateDebounce.restart(); }
            @Override public void changedUpdate(DocumentEvent e) { validateDebounce.restart(); }
        });
    }

    public void loadNode(String nodeId) {
        this.currentNodeId = nodeId;
        playwrightRows.clear();
        SwingWorker<JSONObject, Void> worker = new SwingWorker<>() {
            @Override protected JSONObject doInBackground() throws Exception {
                return client.getLocators(nodeId);
            }
            @Override protected void done() {
                try {
                    JSONObject resp = get();
                    model.setCandidates(resp.optJSONArray("candidates"));
                } catch (Exception ignored) {
                }
            }
        };
        worker.execute();
    }

    /** Returns the user's currently-chosen candidate (top-ranked unique, or row 0). */
    public JSONObject getChosenCandidate() {
        return model.preferred();
    }

    public boolean isPlaywrightDisplay(int row) {
        return playwrightRows.contains(row);
    }

    private void runValidate() {
        String strategy = (String) strategyBox.getSelectedItem();
        String value = customField.getText();
        if (value == null || value.isBlank()) {
            validateBadge.setText(" ");
            return;
        }
        SwingWorker<JSONObject, Void> w = new SwingWorker<>() {
            @Override protected JSONObject doInBackground() throws Exception {
                return client.validate(strategy, value);
            }
            @Override protected void done() {
                try {
                    JSONObject r = get();
                    Integer matches = r.has("match_count") && !r.isNull("match_count")
                            ? r.getInt("match_count") : null;
                    if (matches == null) validateBadge.setText("⚠ unknown");
                    else if (matches == 1) validateBadge.setText("✓ unique");
                    else if (matches == 0) validateBadge.setText("⚠ not found");
                    else validateBadge.setText("✗ " + matches + " matches");
                } catch (Exception ex) {
                    validateBadge.setText("err");
                }
            }
        };
        w.execute();
    }

    // ---- table model -----------------------------------------------------

    private final class CandidateTable extends AbstractTableModel {
        private JSONArray cands = new JSONArray();
        private final String[] cols = {"Badge", "Strategy", "Locator"};

        void setCandidates(JSONArray cs) {
            this.cands = cs == null ? new JSONArray() : cs;
            fireTableDataChanged();
        }

        JSONObject preferred() {
            // Pick the first unique, else row 0.
            for (int i = 0; i < cands.length(); i++) {
                JSONObject c = cands.getJSONObject(i);
                if (c.optBoolean("is_unique", false)) return c;
            }
            return cands.length() > 0 ? cands.getJSONObject(0) : null;
        }

        @Override public int getRowCount() { return cands.length(); }
        @Override public int getColumnCount() { return cols.length; }
        @Override public String getColumnName(int col) { return cols[col]; }

        @Override public Object getValueAt(int row, int col) {
            JSONObject c = cands.getJSONObject(row);
            return switch (col) {
                case 0 -> badge(c);
                case 1 -> c.optString("strategy");
                case 2 -> playwrightRows.contains(row)
                        ? c.optString("playwright")
                        : c.optString("selenium");
                default -> "";
            };
        }

        private String badge(JSONObject c) {
            boolean unique = c.has("is_unique") && !c.isNull("is_unique")
                    && c.getBoolean("is_unique");
            // A positional locator is unique but ORDER-DEPENDENT: it breaks the
            // moment the list re-sorts, filters or pages. Never show it with the
            // same green tick as a stable locator.
            if (unique && c.optBoolean("is_positional", false)) {
                int idx = c.isNull("position_index") ? 0 : c.optInt("position_index", 0);
                return "#" + (idx + 1);
            }
            if (unique) return "✓";
            if (!c.has("match_count") || c.isNull("match_count")) return "?";
            int n = c.getInt("match_count");
            return n == 0 ? "⚠0" : "✗" + n;
        }

        String tooltipFor(int row) {
            if (row < 0 || row >= cands.length()) return null;
            JSONObject c = cands.getJSONObject(row);
            if (c.optBoolean("is_positional", false)) {
                return "<html><b>Order-dependent locator.</b><br>"
                     + "No stable attribute distinguishes this element from its "
                     + "siblings, so it is pinned by index.<br>"
                     + "It will silently target a <i>different</i> element if the "
                     + "list is re-sorted, filtered or paged.<br>"
                     + "Prefer asking the developers for a data-testid.</html>";
            }
            if (!c.isNull("match_count") && c.optInt("match_count", -1) == 0) {
                return "Does not resolve against the live page.";
            }
            int n = c.optInt("match_count", -1);
            if (n > 1) return n + " elements match — not unique.";
            return null;
        }
    }

    private static final class BadgeRenderer extends DefaultListCellRendererStub implements TableCellRenderer {
        @Override public Component getTableCellRendererComponent(JTable table, Object value,
                                                                 boolean isSelected, boolean hasFocus,
                                                                 int row, int column) {
            JLabel l = new JLabel(String.valueOf(value));
            l.setOpaque(true);
            l.setHorizontalAlignment(SwingConstants.CENTER);
            String v = String.valueOf(value);
            if ("✓".equals(v)) l.setForeground(new Color(0, 130, 0));
            // Amber: unique, but pinned by index — usable with caution.
            else if (v.startsWith("#")) l.setForeground(new Color(190, 120, 0));
            else if (v.startsWith("✗") || v.startsWith("⚠")) l.setForeground(new Color(180, 0, 0));
            else l.setForeground(Color.GRAY);
            if (isSelected) l.setBackground(table.getSelectionBackground());
            else l.setBackground(table.getBackground());
            return l;
        }
    }

    /** Placeholder base so the renderer compiles independently. */
    private static class DefaultListCellRendererStub extends JComponent {}
}

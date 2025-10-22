#!/usr/bin/env python3

"""
A Toplevel Tkinter window for inspecting the data loaded
by the RulesetLoader class.

This allows for debugging the YAML parsing and object creation
process by providing a live view of the loaded data structures.

Changes:
- Reorganized the navigation tree to group all entities by a
  supertype -> type -> subtype hierarchy, providing a more
  logical view of the game world's data.
- Added separate top-level navigation trees for Attributes and Skills.
- Implemented a more robust method for retrieving item data on selection,
  using a dictionary to map tree node IDs directly to data objects.
- Retained the clean, collapsible detail view that filters empty values.
- Modified detail view to show 'tags' as a comma-separated list instead
  of an expandable tree.
- Detail view for Attributes now directly lists skills by name.
- Skills and Specializations are now displayed by name, not index.
- Changed "Value" column header to "Description" for clarity.
- Skill/Specialization descriptions now appear in the Description column.
"""

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Any, Union, Dict, List
from dataclasses import is_dataclass, asdict

# Use type checking to avoid circular import issues
if TYPE_CHECKING:
    from classes import RulesetLoader, Entity

class DebugWindow(tk.Toplevel):
    """
    A separate window that displays all data from a RulesetLoader instance.
    """

    def __init__(self, parent: tk.Tk, loader: "RulesetLoader"):
        """
        Initializes the debug window.

        Args:
            parent: The root Tkinter window.
            loader: The fully-loaded RulesetLoader instance to inspect.
        """
        super().__init__(parent)
        self.title("Ruleset Data Inspector")
        self.geometry("1000x700")

        self.loader = loader
        # Maps Treeview node IDs to the actual data objects for quick retrieval
        self.node_data_map = {}
        
        # Main layout is a resizable paned window
        paned_window = ttk.PanedWindow(self, orient='horizontal')
        paned_window.pack(fill='both', expand=True, padx=10, pady=10)

        # --- Left Pane: Navigation Tree ---
        nav_frame = ttk.Frame(paned_window)
        ttk.Label(nav_frame, text="Loaded Elements", font=("-weight bold")).pack(pady=(0, 5))
        self.nav_tree = ttk.Treeview(nav_frame, selectmode='browse')
        self.nav_tree.pack(fill='both', expand=True)
        paned_window.add(nav_frame, weight=1)

        # --- Right Pane: Detail View Tree ---
        detail_frame = ttk.Frame(paned_window)
        ttk.Label(detail_frame, text="Element Details", font=("-weight bold")).pack(pady=(0, 5))
        
        self.detail_tree = ttk.Treeview(detail_frame, columns=('value',), selectmode='browse')
        self.detail_tree.heading('#0', text='Attribute')
        self.detail_tree.heading('value', text='Description')
        self.detail_tree.column('value', width=250)
        self.detail_tree.pack(fill='both', expand=True)
        paned_window.add(detail_frame, weight=3)
        
        # Bind selection event for the navigation tree
        self.nav_tree.bind('<<TreeviewSelect>>', self._on_nav_item_select)

        # Populate the tree with data from the loader
        self._populate_nav_tree()

    def _build_entity_hierarchy(self) -> Dict:
        """
        Combines all entities and organizes them into a nested dictionary
        based on their supertype, type, and subtype.
        """
        hierarchy = {}
        # Combine all entity types into a single list
        all_entities: List[Entity] = (
            list(self.loader.characters.values()) +
            list(self.loader.creatures.values()) +
            list(self.loader.items.values()) +
            list(self.loader.supernatural.values())
        )

        for entity in all_entities:
            # Use placeholders for uncategorized items
            supertype = entity.supertype or "Uncategorized Supertype"
            type_ = entity.type or "Uncategorized Type"
            subtype = entity.subtype or "Uncategorized Subtype"
            
            # Create nested dictionaries on the fly
            hierarchy.setdefault(supertype, {}).setdefault(type_, {}).setdefault(subtype, []).append(entity)
        
        return hierarchy

    def _populate_nav_tree(self):
        """
        Fills the navigation tree using the new hierarchical structure.
        """
        # 1. Populate the hierarchical entity tree
        entity_hierarchy = self._build_entity_hierarchy()

        for supertype, types in sorted(entity_hierarchy.items()):
            supertype_node = self.nav_tree.insert('', 'end', text=supertype, open=False)
            for type_, subtypes in sorted(types.items()):
                type_node = self.nav_tree.insert(supertype_node, 'end', text=type_, open=False)
                for subtype, entities in sorted(subtypes.items()):
                    subtype_node = self.nav_tree.insert(type_node, 'end', text=subtype, open=False)
                    # Sort entities by name before inserting
                    for entity in sorted(entities, key=lambda e: e.name):
                        entity_node_id = self.nav_tree.insert(subtype_node, 'end', text=entity.name)
                        # Map the node's ID directly to the entity object
                        self.node_data_map[entity_node_id] = entity

        # 2. Add Attributes as a separate top-level category
        attributes_data = self.loader.attributes
        if attributes_data:
            attr_parent = self.nav_tree.insert('', 'end', text="Attributes", open=False)
            # Assuming attributes are a list of dicts, each with a 'name'
            sorted_attrs = sorted(attributes_data, key=lambda x: x.get('name', ''))
            for attr_dict in sorted_attrs:
                name = attr_dict.get('name', 'Unnamed Attribute')
                attr_node_id = self.nav_tree.insert(attr_parent, 'end', text=name)
                self.node_data_map[attr_node_id] = attr_dict

        # 3. Add Skills as a separate top-level category
        skills_data = self.loader.skills
        if skills_data:
            skill_parent = self.nav_tree.insert('', 'end', text="Skills", open=False)
            
            # --- FIX ---
            # This logic now mirrors the 'Attributes' logic above,
            # assuming skills is a list of dicts with a 'name' key,
            # which matches the multi-doc YAML format.
            sorted_skills = sorted(skills_data, key=lambda x: x.get('name', ''))
            for skill_dict in sorted_skills:
                name = skill_dict.get('name', 'Unnamed Skill')
                skill_node_id = self.nav_tree.insert(skill_parent, 'end', text=name)
                self.node_data_map[skill_node_id] = skill_dict
            # --- END FIX ---

    def _on_nav_item_select(self, event: tk.Event):
        """
        Callback for when an item in the navigation tree is selected.
        """
        selected_id = self.nav_tree.focus()
        if not selected_id:
            return

        data_obj = self.node_data_map.get(selected_id)

        if data_obj:
            # Check if the selected item is an attribute from the nav tree
            parent_id = self.nav_tree.parent(selected_id)
            is_attribute = parent_id and self.nav_tree.item(parent_id, 'text') == "Attributes"
            
            self._populate_detail_tree(data_obj, is_attribute=is_attribute)
        else:
            self._clear_detail_tree()

    def _clear_detail_tree(self):
        """Clears all items from the detail tree."""
        for item in self.detail_tree.get_children():
            self.detail_tree.delete(item)

    def _populate_detail_tree(self, data: Any, is_attribute: bool = False):
        """
        Clears and populates the detail tree with the provided data,
        filtering out empty values.
        """
        self._clear_detail_tree()

        if is_dataclass(data):
            data = asdict(data)

        # If it's an attribute, we only want to show its skills list
        if is_attribute and isinstance(data, dict) and 'skills' in data:
            self._add_detail_children('', data['skills'])
        else:
            self._add_detail_children('', data)

    def _add_detail_children(self, parent_node: str, data: Any):
        """
        Recursively adds non-empty data to the detail tree view.
        """
        # Special handling for lists of named dicts (like skills in attributes.yaml)
        if isinstance(data, list) and data and isinstance(data[0], dict) and 'name' in data[0]:
            for item in data:
                name = item.get('name', 'Unnamed Item')
                details = item.copy()
                details.pop('name', None)
                description = details.pop('description', '')  # Pop description to show in the value column

                is_empty_sub_object = all(
                    sub_val in (None, False, 0, 0.0, "", [], {})
                    for sub_val in details.values()
                )

                if is_empty_sub_object:
                    self.detail_tree.insert(parent_node, 'end', text=name, values=(description,))
                else:
                    node = self.detail_tree.insert(parent_node, 'end', text=name, open=False, values=(description,))
                    self._add_detail_children(node, details)
            return

        # Standard handling for dictionaries and other lists
        if not isinstance(data, (dict, list)):
            return

        iterator = data.items() if isinstance(data, dict) else enumerate(data)

        for key, value in iterator:
            if value in (None, False, 0, 0.0, "", [], {}):
                continue
            
            if isinstance(value, dict):
                is_empty_sub_object = all(
                    sub_val in (None, False, 0, 0.0, "", [], {})
                    for sub_val in value.values()
                )
                if is_empty_sub_object:
                    continue

            if key == 'tags' and isinstance(value, list):
                self.detail_tree.insert(parent_node, 'end', text='tags', values=(', '.join(map(str, value)),))
                continue

            if isinstance(value, (dict, list)):
                node = self.detail_tree.insert(parent_node, 'end', text=str(key), open=False)
                self._add_detail_children(node, value)
            else:
                self.detail_tree.insert(parent_node, 'end', text=str(key), values=(str(value),))
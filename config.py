from main import *
import __builtin__

def InventoryItem_MultiTool_use(params, item):
	WINDOW.player.inventory.add(item)
	WINDOW.model.remove_block(params)
from __future__ import unicode_literals
import frappe
from frappe.utils import cstr
from frappe import _
import requests.exceptions
from .exceptions import woocommerceError
from .utils import make_woocommerce_log, disable_woocommerce_sync_for_item
from erpnext.stock.utils import get_bin
from frappe.utils import cstr, flt, cint, get_files_path
from .woocommerce_requests import post_request, get_woocommerce_items,get_woocommerce_item_variants,  put_request, get_woocommerce_item_image
import base64, requests, datetime, os

woocommerce_variants_attr_list = ["option1", "option2", "option3"]

def sync_products(price_list, warehouse):
    woocommerce_item_list = []
    #sync_woocommerce_items(warehouse, woocommerce_item_list)
    frappe.local.form_dict.count_dict["products"] = len(woocommerce_item_list)
    sync_erpnext_items(price_list, warehouse, woocommerce_item_list)

def has_variants(woocommerce_item):
    if len(woocommerce_item.get("variations")) >= 1:
        return True
    return False

def create_attribute(woocommerce_item):
    attribute = []
    # woocommerce item dict
    for attr in woocommerce_item.get('attributes'):
        if not frappe.db.get_value("Item Attribute", attr.get("name"), "name"):
            frappe.get_doc({
                "doctype": "Item Attribute",
                "attribute_name": attr.get("name"),
                "woocommerce_attribute_id": attr.get("id"),
                "item_attribute_values": [
                    {
                        "attribute_value": attr_value,
                        "abbr":attr_value
                    }
                    for attr_value in attr.get("options")
                ]
            }).insert()
            attribute.append({"attribute": attr.get("name")})
        else:
            # check for attribute values
            item_attr = frappe.get_doc("Item Attribute", attr.get("name"))
            if not item_attr.numeric_values:
                if not item_attr.get("woocommerce_attribute_id"):
                                item_attr.woocommerce_attribute_id = attr.get("id")
                set_new_attribute_values(item_attr,  attr.get("options"))
                item_attr.save()
                attribute.append({"attribute": attr.get("name")})

            #else:
                #attribute.append({
                    #"attribute": attr.get("name"),
                    #"from_range": item_attr.get("from_range"),
                    #"to_range": item_attr.get("to_range"),
                    #"increment": item_attr.get("increment"),
                    #"numeric_values": item_attr.get("numeric_values")
                #})

    return attribute

def set_new_attribute_values(item_attr, values):
    for attr_value in values:
        if not any((d.abbr.lower() == attr_value.lower() or d.attribute_value.lower() == attr_value.lower())\
        for d in item_attr.item_attribute_values):
            item_attr.append("item_attribute_values", {
                "attribute_value": attr_value,
                "abbr": attr_value
            })

def get_attribute_value(variant_attr_val, attribute):
    attribute_value = frappe.db.sql("""select attribute_value from `tabItem Attribute Value`
        where parent = %s and (abbr = %s or attribute_value = %s)""", (attribute["name"], variant_attr_val,
        variant_attr_val), as_list=1)
    return attribute_value[0][0] if len(attribute_value)>0 else cint(variant_attr_val)

def get_item_group(product_type=None):
    #woocommerce supports multiple categories per item, so we just pick a default in ERPNext
    woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")
    return woocommerce_settings.get("default_item_group")

def add_to_price_list(item, name):
    price_list = frappe.db.get_value("WooCommerce Config", None, "price_list")
    item_price_name = frappe.db.get_value("Item Price",
        {"item_code": name, "price_list": price_list}, "name")

    if not item_price_name:
        frappe.get_doc({
            "doctype": "Item Price",
            "price_list": price_list,
            "item_code": name,
            "price_list_rate": item.get("price") or item.get("item_price")
        }).insert()
    else:
        item_rate = frappe.get_doc("Item Price", item_price_name)
        item_rate.price_list_rate = item.get("price")  or item.get("item_price")
        item_rate.save()

def get_item_image(woocommerce_item):
    if woocommerce_item.get("images"):
        for image in woocommerce_item.get("images"):
            if image.get("position") == 0: # the featured image
                return image
            return None
    else:
        return None

def get_item_details(woocommerce_item):
    item_details = {}

    item_details = frappe.db.get_value("Item", {"woocommerce_id": woocommerce_item.get("id")},
        ["name", "stock_uom", "item_name"], as_dict=1)                                                                      #woocommerce_product_id

    if item_details:
        return item_details

    else:
        item_details = frappe.db.get_value("Item", {"woocommerce_variant_id": woocommerce_item.get("id")},
            ["name", "stock_uom", "item_name"], as_dict=1)
        return item_details

def get_woocommerce_id(woocommerce_item_list):
    return woocommerce_item_list.get('woocommerce_id')

def sync_erpnext_items(price_list, warehouse, woocommerce_item_list):
    woocommerce_item_list = {}
    woocommerce_item_list.sort(key=get_woocommerce_id)
    for item in get_woocommerce_items():
        woocommerce_item_list[int(item['id'])] = item

    for item in get_erpnext_items(price_list):
        #if item.sync_qty_with_woocommerce and item.sync_with_woocommerce: #jupiter - additional - SEPTEMBER 9, 2020 skip products disabled syncs
        try:
            sync_item_with_woocommerce(item, price_list, warehouse, woocommerce_item_list.get(item.get('woocommerce_id'))) #woocommerce_product_id
            frappe.local.form_dict.count_dict["products"] += 1

        except woocommerceError as e:
            make_woocommerce_log(title=e.message + " " + woocommerce_item_list.get(item.get('woocommerce_id')), status="Error", method="sync_woocommerce_items", message=frappe.get_traceback(),
                request_data=item, exception=True)
        except Exception as e:
            make_woocommerce_log(title=e.message, status="Error", method="sync_woocommerce_items", message=frappe.get_traceback(),
                request_data=item, exception=True)

def get_erpnext_items(price_list):
    erpnext_items = []
    woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")

    last_sync_condition, item_price_condition = "", ""
    if woocommerce_settings.last_sync_datetime:
        last_sync_condition = "and modified >= '{0}' ".format(woocommerce_settings.last_sync_datetime)
        item_price_condition = "and ip.modified >= '{0}' ".format(woocommerce_settings.last_sync_datetime)

    item_from_master = """select name, item_code, item_name, item_group,
        description, woocommerce_description, has_variants, variant_of, stock_uom, image, woocommerce_id,
        woocommerce_variant_id, sync_qty_with_woocommerce, weight_per_unit, weight_uom from tabItem
        where sync_with_woocommerce=1 and (variant_of is null or variant_of = '')
        and (disabled is null or disabled = 0)  %s """ % last_sync_condition                                            #woocommerce_product_id

    erpnext_items.extend(frappe.db.sql(item_from_master, as_dict=1))

    template_items = [item.name for item in erpnext_items if item.has_variants]

    if len(template_items) > 0:
        item_price_condition += ' and i.variant_of not in (%s)'%(' ,'.join(["'%s'"]*len(template_items)))%tuple(template_items)

    item_from_item_price = """select i.name, i.item_code, i.item_name, i.item_group, i.description,
        i.woocommerce_description, i.has_variants, i.variant_of, i.stock_uom, i.image, i.woocommerce_id,
        i.woocommerce_variant_id, i.sync_qty_with_woocommerce, i.weight_per_unit, i.weight_uom
        from `tabItem` i, `tabItem Price` ip
        where price_list = '%s' and i.name = ip.item_code
            and sync_with_woocommerce=1 and (disabled is null or disabled = 0) %s""" %(price_list, item_price_condition)                #woocommerce_product_id

    updated_price_item_list = frappe.db.sql(item_from_item_price, as_dict=1)

    # to avoid item duplication
    return [frappe._dict(tupleized) for tupleized in set(tuple(item.items())
        for item in erpnext_items + updated_price_item_list)]

def sync_item_with_woocommerce(item, price_list, warehouse, woocommerce_item=None):
    variant_item_name_list = []
    variant_list = []
    wc_product_category_id = frappe.db.get_value(
        "Item Group", item.item_group, "woocommerce_id_za") #jupiter - additional
    
    woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")                                                       #jupiter additional
    if woocommerce_settings.sync_itemgroup_to_wp_categories:                                                                                #optional syncing of categories
        item_data = {
                "name": item.get("item_name"),
                "description": item.get("woocommerce_description") or item.get("web_long_description") or item.get("description"),
                "short_description": item.get("woocommerce_description") or item.get("web_long_description") or item.get("description"),
                "sku": item.get("item_code"),                                                                                                #jupiter - additional
                "categories": [
                    {
                        "id": wc_product_category_id                                                                                        #jupiter - additional
                    }
                ],                                                      
        }
    else:
        item_data = {
                "name": item.get("item_name"),
                "description": item.get("woocommerce_description") or item.get("web_long_description") or item.get("description"),
                "short_description": item.get("woocommerce_description") or item.get("web_long_description") or item.get("description"),
                "sku": item.get("item_code"),                                                                                                #jupiter - additional                                                     
        }
        
        
    item_data.update( get_price_and_stock_details(item, warehouse, price_list) )

    if item.get("has_variants"):  # we are dealing a variable product
        item_data["type"] = "variable"

        if item.get("variant_of"):
            item = frappe.get_doc("Item", item.get("variant_of"))

        variant_list, options, variant_item_name = get_variant_attributes(item, price_list, warehouse)
        item_data["attributes"] = options

    else:   # we are dealing with a simple product
        item_data["type"] = "simple"


    erp_item = frappe.get_doc("Item", item.get("name"))
    erp_item.flags.ignore_mandatory = True

    if not item.get("woocommerce_id"):                                                                      #woocommerce_product_id
        item_data["status"] = "draft"
        create_new_item_to_woocommerce(item, item_data, erp_item, variant_item_name_list)
    elif not item.get("woocommerce_id").isnumeric():
        item_data["status"] = "draft"    
        create_new_item_to_woocommerce(item, item_data, erp_item, variant_item_name_list)

    else:
        item_data["id"] = item.get("woocommerce_id")                                                        #woocommerce_product_id
        try:
            put_request("products/{0}".format(item.get("woocommerce_id")), item_data)                       #woocommerce_product_id

        except requests.exceptions.HTTPError as e:
            if e.args[0] and (e.args[0].startswith("404") or e.args[0].startswith("400")):
                if frappe.db.get_value("WooCommerce Config", "WooCommerce Config", "if_not_exists_create_item_to_woocommerce"):
                    item_data["id"] = ''
                    create_new_item_to_woocommerce(item, item_data, erp_item, variant_item_name_list)
                else:
                    disable_woocommerce_sync_for_item(erp_item)
            else:
                make_woocommerce_log(title=e, status="Error", method="sync_products.sync_item_with_woocommerce", message=frappe.get_traceback(),
                request_data=item_data, exception=True)
                raise e

    if variant_list:
        for variant in variant_list:
            erp_varient_item = frappe.get_doc("Item", variant["item_name"])
            if erp_varient_item.woocommerce_id: #varient exist in woocommerce let's update only #woocommerce_product_id
                r = put_request("products/{0}/variations/{1}".format(erp_item.woocommerce_id, erp_varient_item.woocommerce_id),variant) #woocommerce_product_id
            else:
                woocommerce_variant = post_request("products/{0}/variations".format(erp_item.woocommerce_id), variant)                  #woocommerce_product_id

                erp_varient_item.woocommerce_id = woocommerce_variant.get("id")                                                         #woocommerce_product_id
                erp_varient_item.woocommerce_variant_id = woocommerce_variant.get("id")
                erp_varient_item.save()

    if erp_item.image:
        try:
            item_image = get_item_image(woocommerce_item)
        except:
            item_image = None
        img_details = frappe.db.get_value("File", {"file_url": erp_item.image}, ["modified"])

        if not item_image or datetime.datetime(item_image.date_modified, '%Y-%m-%dT%H:%M:%S') < datetime.datetime(img_details[0], '%Y-%m-%d %H:%M:%S.%f'):
            sync_item_image(erp_item)

    frappe.db.commit()


def create_new_item_to_woocommerce(item, item_data, erp_item, variant_item_name_list):
    new_item = post_request("products", item_data)

    erp_item.woocommerce_id = new_item.get("id")                                                                #woocommerce_product_id

    #if not item.get("has_variants"):
        #erp_item.woocommerce_variant_id = new_item['product']["variants"][0].get("id")

    erp_item.save()
    #update_variant_item(new_item, variant_item_name_list)

def sync_item_image(item):
    image_info = {
        "images": [{}]
    }

    if item.image:
        img_details = frappe.db.get_value("File", {"file_url": item.image}, ["file_name", "file_url", "is_private", "content_hash"])

        image_info["images"][0]["src"] = 'https://' + cstr(frappe.local.site) + img_details[1]
        image_info["images"][0]["position"] = 0

        post_request("products/{0}".format(item.woocommerce_id), image_info)                                    #woocommerce_product_id


def validate_image_url(url):
    """ check on given url image exists or not"""
    res = requests.get(url)
    if res.headers.get("content-type") in ('image/png', 'image/jpeg', 'image/gif', 'image/bmp', 'image/tiff'):
        return True
    return False

def item_image_exists(woocommerce_id, image_info):                                                              #woocommerce_product_id
    """check same image exist or not"""
    for image in get_woocommerce_item_image(woocommerce_id):                                                    #woocommerce_product_id
        if image_info.get("image").get("filename"):
            if os.path.splitext(image.get("src"))[0].split("/")[-1] == os.path.splitext(image_info.get("image").get("filename"))[0]:
                return True
        elif image_info.get("image").get("src"):
            if os.path.splitext(image.get("src"))[0].split("/")[-1] == os.path.splitext(image_info.get("image").get("src"))[0].split("/")[-1]:
                return True
        else:
            return False

def update_variant_item(new_item, item_code_list):
    for i, name in enumerate(item_code_list):
        erp_item = frappe.get_doc("Item", name)
        erp_item.flags.ignore_mandatory = True
        erp_item.woocommerce_id = new_item['product']["variants"][i].get("id")                              #woocommerce_product_id
        erp_item.woocommerce_variant_id = new_item['product']["variants"][i].get("id")
        erp_item.save()

def get_variant_attributes(item, price_list, warehouse):
    options, variant_list, variant_item_name, attr_sequence = [], [], [], []
    attr_dict = {}

    for i, variant in enumerate(frappe.get_all("Item", filters={"variant_of": item.get("name")},
        fields=['name'])):

        item_variant = frappe.get_doc("Item", variant.get("name"))

        data = (get_price_and_stock_details(item_variant, warehouse, price_list))
        data["item_name"] = item_variant.name
        data["attributes"] = []
        for attr in item_variant.get('attributes'):
            attribute_option = {}
            attribute_option["name"] = attr.attribute
            attribute_option["option"] = attr.attribute_value
            data["attributes"].append(attribute_option)

            if attr.attribute not in attr_sequence:
                attr_sequence.append(attr.attribute)
            if not attr_dict.get(attr.attribute):
                attr_dict.setdefault(attr.attribute, [])

            attr_dict[attr.attribute].append(attr.attribute_value)

        variant_list.append(data)


    for i, attr in enumerate(attr_sequence):
        options.append({
            "name": attr,
            "visible": "True",
            "variation": "True",
            "position": i+1,
            "options": list(set(attr_dict[attr]))
        })
    return variant_list, options, variant_item_name

def get_price_and_stock_details(item, warehouse, price_list):
    qty = frappe.db.get_value("Bin", {"item_code":item.get("item_code"), "warehouse": warehouse}, "actual_qty")
    price = frappe.db.get_value("Item Price", \
            {"price_list": price_list, "item_code":item.get("item_code")}, "price_list_rate")

    item_price_and_quantity = {
        "regular_price": "{0}".format(flt(price)) #only update regular price
    }

    if item.weight_per_unit:
        if item.weight_uom and item.weight_uom.lower() in ["kg", "g", "oz", "lb", "lbs"]:
            item_price_and_quantity.update({
                "weight": "{0}".format(get_weight_in_woocommerce_unit(item.weight_per_unit, item.weight_uom))
            })

    if item.stock_keeping_unit:
        item_price_and_quantity = {
        "sku": "{0}".format(item.stock_keeping_unit)
    }

    if item.get("sync_qty_with_woocommerce"):
        item_price_and_quantity.update({
            "stock_quantity": "{0}".format(cint(qty) if qty else 0),
            "manage_stock": "True"
        })

    #rlavaud Do I need this???
    if item.woocommerce_variant_id:
        item_price_and_quantity["id"] = item.woocommerce_variant_id


    return item_price_and_quantity

def get_weight_in_grams(weight, weight_uom):
    convert_to_gram = {
        "kg": 1000,
        "lb": 453.592,
        "oz": 28.3495,
        "g": 1
    }

    return weight * convert_to_gram[weight_uom.lower()]

def get_weight_in_woocommerce_unit(weight, weight_uom):
    woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")
    weight_unit = woocommerce_settings.weight_unit
    convert_to_gram = {
        "kg": 1000,
        "lb": 453.592,
        "lbs": 453.592,
        "oz": 28.3495,
        "g": 1
    }
    convert_to_oz = {
        "kg": 0.028,
        "lb": 0.062,
        "lbs": 0.062,
        "oz": 1,
        "g": 28.349
    }
    convert_to_lb = {
        "kg": 1000,
        "lb": 1,
        "lbs": 1,
        "oz": 16,
        "g": 0.453
    }
    convert_to_kg = {
        "kg": 1,
        "lb": 2.205,
        "lbs": 2.205,
        "oz": 35.274,
        "g": 1000
    }
    if weight_unit.lower() == "g":
        return weight * convert_to_gram[weight_uom.lower()]

    if weight_unit.lower() == "oz":
        return weight * convert_to_oz[weight_uom.lower()]

    if weight_unit.lower() == "lb"  or weight_unit.lower() == "lbs":
        return weight * convert_to_lb[weight_uom.lower()]

    if weight_unit.lower() == "kg":
        return weight * convert_to_kg[weight_uom.lower()]



def trigger_update_item_stock(doc, method):
    if doc.flags.via_stock_ledger_entry:
        woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")
        if woocommerce_settings.woocommerce_url and woocommerce_settings.enable_woocommerce:
            try:
                update_item_stock(doc.item_code, woocommerce_settings, doc)
                make_woocommerce_log(title="WooCommerce Config woocommerceconnector.trigger_update_item_stock", status="Success", method="woocommerceconnector.trigger_update_item_stock", message=frappe.get_traceback(), request_data=doc.item_code, exception=True)
            except Exception as e:
                make_woocommerce_log(title="WooCommerce Config: " + e, status="Error", method="trigger_update_item_stock", message=frappe.get_traceback(), request_data=doc.item_code, exception=True)
            

def update_item_stock_qty():
    woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")

    for item in frappe.get_all("Item", fields=["item_code"], filters={"sync_qty_with_woocommerce": '1', "disabled": ("!=", 1)}):
        try:
            update_item_stock(item.item_code, woocommerce_settings)
        except woocommerceError as e:
            make_woocommerce_log(title=e, status="Error", method="sync_woocommerce_items", message=frappe.get_traceback(),
                request_data=item, exception=True)

        except Exception as e:
            if e.args[0] and e.args[0].startswith("402"):
                raise e
            else:
                make_woocommerce_log(title=e, status="Error", method="sync_woocommerce_items", message=frappe.get_traceback(),
                    request_data=item, exception=True)

def update_item_stock(item_code, woocommerce_settings, bin=None):
    item = frappe.get_doc("Item", item_code)
    if item.sync_qty_with_woocommerce and item.sync_with_woocommerce:                                           #added  and item.sync_with_woocommerce
        if not item.woocommerce_id:                                                                             #woocommerce_product_id
            make_woocommerce_log(title="WooCommerce ID missing", status="Error", method="sync_woocommerce_items",
                message="Please sync WooCommerce IDs to ERP (missing for item {0})".format(item_code), request_data=item_code, exception=True)
        else:
            bin = get_bin(item_code, woocommerce_settings.warehouse)
            qty = bin.actual_qty
            for warehouse in woocommerce_settings.warehouses:
                _bin = get_bin(item_code, warehouse.warehouse)
                qty += _bin.actual_qty

            # bugfix #1582: variant control from WooCommerce, not ERPNext
            if item.woocommerce_variant_id and int(item.woocommerce_variant_id) > 0:
                item_data, resource = get_product_update_dict_and_resource(item.woocommerce_id, item.woocommerce_variant_id, is_variant=True, actual_qty=qty)   #woocommerce_product_id
            else:
                item_data, resource = get_product_update_dict_and_resource(item.woocommerce_id, item.woocommerce_variant_id, actual_qty=qty)                    #woocommerce_product_id
            
            try:
                make_woocommerce_log(title="Update stock of {0}({1})".format(item.item_name, item.item_code), status="Success", method="woocommerceconnector.sync_products.update_item_stock", message="Resource: {0}, data: {1}".format(resource, item_data))
                put_request(resource, item_data)
            except requests.exceptions.HTTPError as e:
                if e.args[0] and e.args[0].startswith("404"):
                    make_woocommerce_log(title=e.message, status="Error", method="woocommerceconnector.sync_products.update_item_stock", message=frappe.get_traceback(),
                        request_data=item_data, exception=True)
                    disable_woocommerce_sync_for_item(item)
                else:
                    raise e


def get_product_update_dict_and_resource(woocommerce_id, woocommerce_variant_id, is_variant=False, actual_qty=0):                                               #woocommerce_product_id
    item_data = {}
    item_data["stock_quantity"] = "{0}".format(cint(actual_qty))
    item_data["manage_stock"] = "1"

    if is_variant:
        resource = "products/{0}/variations/{1}".format(woocommerce_id,woocommerce_variant_id)                                                                  #woocommerce_product_id
    else: #simple item
        resource = "products/{0}".format(woocommerce_id)                                                                                                        #woocommerce_product_id

    return item_data, resource

def add_w_id_to_erp():
    # purge WooCommerce IDs so that there cannot be any conflict
    purge_ids = """UPDATE `tabItem`
            SET `woocommerce_id` = NULL, `woocommerce_variant_id` = NULL;""" #woocommerce_product_id
    frappe.db.sql(purge_ids)
    frappe.db.commit()

    # loop through all items on WooCommerce and get their IDs (matched by barcode)
    woo_items = get_woocommerce_items()
    make_woocommerce_log(title="Syncing IDs", status="Started", method="add_w_id_to_erp", message='Item: {0}'.format(woo_items),
        request_data={}, exception=True)
    for woocommerce_item in woo_items:
        update_item = """UPDATE `tabItem`
            SET `woocommerce_id` = '{0}', `ugs` = '{1}' 
            WHERE `item_code` = '{1}';""".format(woocommerce_item.get("id"), woocommerce_item.get("sku")) #woocommerce_product_id
        frappe.db.sql(update_item)
        frappe.db.commit()
        
        #####REMOVED BY JUPITER - NOT NEEDED CODE
        #for woocommerce_variant in get_woocommerce_item_variants(woocommerce_item.get("id")):
        #    update_variant = """UPDATE `tabItem`
        #        SET `woocommerce_variant_id` = '{0}', `woocommerce_id` = '{1}', `ugs` = '{1}'
        #        WHERE `item_code` = '{1}';""".format(woocommerce_variant.get("id"), woocommerce_item.get("sku")) #woocommerce_product_id
        #    frappe.db.sql(update_variant)
        #    frappe.db.commit()
        
    make_woocommerce_log(title="IDs synced", status="Success", method="woocommerceconnector.sync_products.add_w_id_to_erp", message={},
        request_data={}, exception=True)

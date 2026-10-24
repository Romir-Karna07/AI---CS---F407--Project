# from playwright.sync_api import sync_playwright
# import json
# import re

# OUTPUT_FILE = "network_capture.jsonl"


# def save_response(response):
#     request = response.request

#     if request.resource_type not in ["xhr", "fetch"]:
#         return

#     try:
#         url = request.url

#         # Focus on potentially relevant API/map requests
#         interesting = any(
#             keyword in url.lower()
#             for keyword in [
#                 "layers",
#                 "soil",
#                 "feature",
#                 "map",
#                 "wms",
#                 "wfs",
#                 "graphql",
#                 "public"
#             ]
#         )

#         if not interesting:
#             return

#         # IMPORTANT: read body immediately
#         try:
#             body_bytes = response.body()

#             try:
#                 body = body_bytes.decode("utf-8")
#             except UnicodeDecodeError:
#                 body = "<binary response>"

#         except Exception as e:
#             body = f"<BODY ERROR: {e}>"

#         record = {
#             "request": {
#                 "method": request.method,
#                 "url": request.url,
#                 "resource_type": request.resource_type,
#                 "headers": dict(request.headers),
#                 "post_data": request.post_data,
#             },
#             "response": {
#                 "status": response.status,
#                 "content_type": response.headers.get(
#                     "content-type", ""
#                 ),
#                 "body": body
#             }
#         }

#         with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
#             f.write(
#                 json.dumps(
#                     record,
#                     ensure_ascii=False
#                 ) + "\n"
#             )

#         print("\n" + "=" * 100)
#         print(request.method, response.status)
#         print(url)

#         if request.post_data:
#             print("\nREQUEST BODY:")
#             print(request.post_data[:3000])

#         print("\nRESPONSE BODY:")
#         print(body[:10000])

#         print("=" * 100)

#     except Exception as e:
#         print("Capture error:", e)


# with sync_playwright() as p:

#     browser = p.chromium.launch(headless=False)

#     context = browser.new_context()

#     page = context.new_page()

#     page.on("response", save_response)

#     print("Opening SLUSI portal...")

#     page.goto(
#         "https://soilhealth.dac.gov.in/slusi-visualisation/",
#         wait_until="domcontentloaded",
#         timeout=60000
#     )

#     page.wait_for_timeout(10000)

#     print("\n" + "=" * 100)
#     print("PORTAL LOADED")
#     print("=" * 100)

#     print("""
# Interact with the portal.

# 1. Select MANIPUR
# 2. Select BISHNUPUR
# 3. Wait for the map/layers to load
# 4. Zoom in
# 5. Click a dot
# 6. Wait 2-3 seconds
# 7. Click another dot if possible
# 8. Return here and press ENTER
# """)

#     input()

#     # Give pending response handlers time to finish
#     page.wait_for_timeout(3000)

#     browser.close()

# print("\nCapture saved to:", OUTPUT_FILE)


from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()

    def request_handler(request):
        url = request.url.lower()

        if any(x in url for x in [
            "layer",
            "soil",
            "shc",
            "wms",
            "wfs",
            "feature",
            "map",
            "arcgis"
        ]):
            print("\nREQUEST:")
            print(request.method, request.url)

            if request.post_data:
                print("BODY:", request.post_data)

    page.on("request", request_handler)

    page.goto(
        "https://soilhealth.dac.gov.in/slusi-visualisation/",
        wait_until="domcontentloaded",
        timeout=60000
    )

    page.wait_for_timeout(10000)

    print("\nSITE OPENED.")
    print("Now select MANIPUR → BISHNUPUR normally.")
    print("Wait for the map to load.")
    print("Then press ENTER here.")

    input()

    browser.close()
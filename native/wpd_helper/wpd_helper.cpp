// wpd_helper.cpp — Stateless WPD command-line helper for Transfera
//
// Talks directly to Windows Portable Devices COM API (no third-party wrappers).
// Invoked as a subprocess from the Python backend, once per operation.
//
// Subcommands:
//   list-devices
//   list-folder --device <id> --path <virtual_path>
//   read-file   --device <id> --path <virtual_path>
//
// Build: see CMakeLists.txt or build.bat in this directory.
// Requires: Windows 10/11 with WPD components (standard install).

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <objbase.h>
#include <shlwapi.h>
#include <propvarutil.h>
#include <stdio.h>
#include <stdlib.h>
#include <io.h>
#include <fcntl.h>
#include <strsafe.h>
#include <new>
#include <string>
#include <vector>
#include <unordered_map>
#include <wrl/client.h>

#include <PortableDeviceApi.h>
#include <PortableDevice.h>

using namespace Microsoft::WRL;

// ---------------------------------------------------------------------------
// Client info constants — matching the official Microsoft WPD sample exactly
// ---------------------------------------------------------------------------
#define CLIENT_NAME             L"Transfera"
#define CLIENT_MAJOR_VER        1
#define CLIENT_MINOR_VER        0
#define CLIENT_REVISION         0

// ---------------------------------------------------------------------------
// RAII COM initializer
// ---------------------------------------------------------------------------
class ComInitGuard {
public:
    ComInitGuard() : m_hr(CoInitializeEx(nullptr, COINIT_MULTITHREADED)) {}
    ~ComInitGuard() { if (SUCCEEDED(m_hr)) CoUninitialize(); }
    HRESULT hr() const { return m_hr; }
    ComInitGuard(const ComInitGuard&) = delete;
    ComInitGuard& operator=(const ComInitGuard&) = delete;
private:
    HRESULT m_hr;
};

// ---------------------------------------------------------------------------
// JSON helpers (minimal, no external library)
// ---------------------------------------------------------------------------
static void JsonEscapeAppend(std::wstring& out, const std::wstring& s) {
    for (wchar_t c : s) {
        switch (c) {
        case L'"':  out += L"\\\""; break;
        case L'\\': out += L"\\\\"; break;
        case L'\n': out += L"\\n"; break;
        case L'\r': out += L"\\r"; break;
        case L'\t': out += L"\\t"; break;
        default:
            if (c < 0x20) {
                wchar_t buf[8];
                swprintf_s(buf, L"\\u%04x", (unsigned int)c);
                out += buf;
            } else {
                out += c;
            }
        }
    }
}

// Write a narrow UTF-8 JSON string to a FILE*
static void WriteJsonString(FILE* fp, const char* key, const std::wstring& value, bool last = false) {
    // Convert wide to UTF-8
    int len = WideCharToMultiByte(CP_UTF8, 0, value.c_str(), (int)value.size(), nullptr, 0, nullptr, nullptr);
    std::string utf8;
    if (len > 0) {
        utf8.resize(len);
        WideCharToMultiByte(CP_UTF8, 0, value.c_str(), (int)value.size(), &utf8[0], len, nullptr, nullptr);
    }

    fprintf(fp, "\"%s\":\"", key);
    // JSON-escape the UTF-8 string
    for (char c : utf8) {
        switch (c) {
        case '"':  fputs("\\\"", fp); break;
        case '\\': fputs("\\\\", fp); break;
        case '\n': fputs("\\n", fp); break;
        case '\r': fputs("\\r", fp); break;
        case '\t': fputs("\\t", fp); break;
        default:
            if ((unsigned char)c < 0x20) {
                fprintf(fp, "\\u%04x", (unsigned int)(unsigned char)c);
            } else {
                fputc(c, fp);
            }
        }
    }
    fputs("\"", fp);
    if (!last) fputc(',', fp);
}

static void WriteJsonNull(FILE* fp, const char* key, bool last = false) {
    fprintf(fp, "\"%s\":null", key);
    if (!last) fputc(',', fp);
}

static void WriteJsonInt(FILE* fp, const char* key, long long value, bool last = false) {
    fprintf(fp, "\"%s\":%lld", key, value);
    if (!last) fputc(',', fp);
}

static void WriteJsonStringOrEmpty(FILE* fp, const char* key, const std::wstring& value, bool last = false) {
    if (value.empty()) {
        WriteJsonNull(fp, key, last);
    } else {
        WriteJsonString(fp, key, value, last);
    }
}

// ---------------------------------------------------------------------------
// Error reporting — always to stderr, never pollutes stdout
// ---------------------------------------------------------------------------
static void ReportError(const char* category, const std::wstring& message, HRESULT hr = S_OK) {
    FILE* fp = stderr;
    fputs("{\"error\":\"", fp);
    fputs(category, fp);
    fputs("\",\"message\":\"", fp);
    // Escape message
    int len = WideCharToMultiByte(CP_UTF8, 0, message.c_str(), (int)message.size(), nullptr, 0, nullptr, nullptr);
    if (len > 0) {
        std::string utf8(len, '\0');
        WideCharToMultiByte(CP_UTF8, 0, message.c_str(), (int)message.size(), &utf8[0], len, nullptr, nullptr);
        for (char c : utf8) {
            switch (c) {
            case '"':  fputs("\\\"", fp); break;
            case '\\': fputs("\\\\", fp); break;
            case '\n': fputs("\\n", fp); break;
            case '\r': fputs("\\r", fp); break;
            default: fputc(c, fp);
            }
        }
    }
    if (hr != S_OK) {
        fprintf(fp, "\",\"hresult\":\"0x%08lx\"", (unsigned long)hr);
    } else {
        fputs("\"", fp);
    }
    fputs("}\n", fp);
}

// ---------------------------------------------------------------------------
// Client information — matches official sample pattern exactly
// ---------------------------------------------------------------------------
static HRESULT GetClientInformation(IPortableDeviceValues** clientInformation) {
    *clientInformation = nullptr;
    ComPtr<IPortableDeviceValues> clientInfo;

    HRESULT hr = CoCreateInstance(CLSID_PortableDeviceValues,
                                  nullptr,
                                  CLSCTX_INPROC_SERVER,
                                  IID_PPV_ARGS(&clientInfo));
    if (FAILED(hr)) return hr;

    hr = clientInfo->SetStringValue(WPD_CLIENT_NAME, CLIENT_NAME);
    if (FAILED(hr)) return hr;

    hr = clientInfo->SetUnsignedIntegerValue(WPD_CLIENT_MAJOR_VERSION, CLIENT_MAJOR_VER);
    if (FAILED(hr)) return hr;

    hr = clientInfo->SetUnsignedIntegerValue(WPD_CLIENT_MINOR_VERSION, CLIENT_MINOR_VER);
    if (FAILED(hr)) return hr;

    hr = clientInfo->SetUnsignedIntegerValue(WPD_CLIENT_REVISION, CLIENT_REVISION);
    if (FAILED(hr)) return hr;

    // SECURITY_IMPERSONATION so we work with all devices (matching official sample)
    hr = clientInfo->SetUnsignedIntegerValue(WPD_CLIENT_SECURITY_QUALITY_OF_SERVICE, SECURITY_IMPERSONATION);
    if (FAILED(hr)) return hr;

    *clientInformation = clientInfo.Detach();
    return S_OK;
}

// ---------------------------------------------------------------------------
// String helper: read a WPD string property (two-call pattern from sample)
// ---------------------------------------------------------------------------
static HRESULT GetDeviceStringProperty(
    IPortableDeviceManager* dm,
    PCWSTR deviceId,
    HRESULT (STDMETHODCALLTYPE IPortableDeviceManager::*getter)(PCWSTR, PWSTR, DWORD*),
    std::wstring& out)
{
    DWORD chars = 0;
    HRESULT hr = (dm->*getter)(deviceId, nullptr, &chars);
    if (FAILED(hr) || chars == 0) {
        out.clear();
        return FAILED(hr) ? hr : S_OK;
    }
    std::vector<WCHAR> buf(chars);
    hr = (dm->*getter)(deviceId, buf.data(), &chars);
    if (SUCCEEDED(hr)) {
        out.assign(buf.data());
    }
    return hr;
}

// Forward declarations
static HRESULT OpenDevice(
    PCWSTR deviceId,
    IPortableDevice** device,
    IPortableDeviceContent** content);

// ---------------------------------------------------------------------------
// Enumerate devices (list-devices subcommand)
// ---------------------------------------------------------------------------
static int DoListDevices() {
    ComPtr<IPortableDeviceManager> dm;

    HRESULT hr = CoCreateInstance(CLSID_PortableDeviceManager, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&dm));
    if (FAILED(hr)) {
        ReportError("com_error", L"Failed to create IPortableDeviceManager", hr);
        return 1;
    }

    // Helper lambda: refresh + fetch device list, returns vector of IDs
    auto fetchDevices = [&](std::vector<PWSTR>& ids) -> DWORD {
        ids.clear();
        DWORD count = 0;
        hr = dm->RefreshDeviceList();
        if (FAILED(hr)) return 0;
        hr = dm->GetDevices(nullptr, &count);
        if (FAILED(hr) || count == 0) return 0;
        ids.resize(count);
        ZeroMemory(ids.data(), count * sizeof(PWSTR));
        DWORD retrieved = count;
        hr = dm->GetDevices(ids.data(), &retrieved);
        if (FAILED(hr)) return 0;
        return retrieved;
    };

    std::vector<PWSTR> deviceIds;
    DWORD retrieved = fetchDevices(deviceIds);

    if (retrieved == 0) {
        fputs("[]\n", stdout);
        return 0;
    }

    // If no device ID has the canonical WPD bus-enumerator prefix
    // (\\?\swd#wpdbusenum#...), the WPD class driver may not have
    // fully claimed the device yet.  Wait briefly and retry once.
    bool hasWpdBusPath = false;
    for (DWORD i = 0; i < retrieved; i++) {
        if (deviceIds[i] && wcsstr(deviceIds[i], L"swd#wpdbusenum")) {
            hasWpdBusPath = true;
            break;
        }
    }

    if (!hasWpdBusPath && retrieved > 0) {
        // Free the first batch
        for (DWORD i = 0; i < retrieved; i++) {
            if (deviceIds[i]) CoTaskMemFree(deviceIds[i]);
        }
        Sleep(1500); // 1.5 s for WPD enumeration to settle
        retrieved = fetchDevices(deviceIds);
    }

    if (retrieved == 0) {
        fputs("[]\n", stdout);
        return 0;
    }

    // Collect all device info into entries
    struct DeviceEntry {
        std::wstring deviceId;
        std::wstring friendlyName;
        std::wstring manufacturer;
    };
    std::vector<DeviceEntry> entries;
    for (DWORD i = 0; i < retrieved; i++) {
        DeviceEntry entry;
        if (deviceIds[i]) entry.deviceId = deviceIds[i];
        GetDeviceStringProperty(dm.Get(), deviceIds[i],
            &IPortableDeviceManager::GetDeviceFriendlyName, entry.friendlyName);
        GetDeviceStringProperty(dm.Get(), deviceIds[i],
            &IPortableDeviceManager::GetDeviceManufacturer, entry.manufacturer);
        entries.push_back(std::move(entry));
    }

    // Free raw PnP ID strings now that we've copied what we need
    for (DWORD i = 0; i < retrieved; i++) {
        if (deviceIds[i]) CoTaskMemFree(deviceIds[i]);
    }
    deviceIds.clear();

    // Filter: when two entries match the same physical device (same
    // friendly_name + manufacturer), prefer the canonical WPD bus-
    // enumerator path over a raw USB enumeration path.  This prevents
    // the same physical device from getting different device IDs in
    // different runs depending on Windows driver-enumeration timing.
    std::vector<bool> keep(entries.size(), true);
    for (size_t i = 0; i < entries.size(); i++) {
        if (!keep[i]) continue;
        bool iIsWpd = entries[i].deviceId.find(L"swd#wpdbusenum") != std::wstring::npos;

        for (size_t j = i + 1; j < entries.size(); j++) {
            if (!keep[j]) continue;
            if (entries[i].friendlyName != entries[j].friendlyName) continue;
            if (entries[i].manufacturer != entries[j].manufacturer) continue;

            bool jIsWpd = entries[j].deviceId.find(L"swd#wpdbusenum") != std::wstring::npos;
            if (iIsWpd && !jIsWpd) {
                keep[j] = false;
            } else if (jIsWpd && !iIsWpd) {
                keep[i] = false;
                break;
            }
        }
    }

    // Filter out mass-storage-only devices.
    //
    // Mass-storage devices (external hard drives, USB flash drives, etc.)
    // are sometimes exposed through WPD via a Windows-driver wrapper that
    // makes their root children look like drive-path references (e.g. an
    // object_id of "E:\" or "D:\").  These should NOT appear in the
    // "Connected devices" UI — they are regular drive letters that the
    // user should browse via "Browse a folder on this PC" instead.
    //
    // To detect this, open each surviving device briefly, enumerate its
    // root children, and check whether any child's object_id matches a
    // Windows drive-path pattern (drive letter + colon + backslash).
    for (size_t i = 0; i < entries.size(); i++) {
        if (!keep[i]) continue;

        ComPtr<IPortableDevice> device;
        ComPtr<IPortableDeviceContent> content;
        HRESULT hrDev = OpenDevice(entries[i].deviceId.c_str(), &device, &content);
        if (FAILED(hrDev)) continue; // Can't open — keep it (conservative)

        ComPtr<IEnumPortableDeviceObjectIDs> rootEnum;
        hrDev = content->EnumObjects(0, WPD_DEVICE_OBJECT_ID, nullptr, &rootEnum);
        if (FAILED(hrDev)) continue;

        bool isMassStorage = false;
        while (hrDev == S_OK && !isMassStorage) {
            DWORD fetched = 0;
            PWSTR objIds[10] = {};
            hrDev = rootEnum->Next(10, objIds, &fetched);
            if (FAILED(hrDev)) break;

            for (DWORD j = 0; j < fetched; j++) {
                if (!objIds[j]) continue;
                std::wstring id(objIds[j]);
                CoTaskMemFree(objIds[j]);

                // Check for Windows absolute path pattern: "X:\" or "X:\something"
                if (id.size() >= 2 &&
                    iswalpha(id[0]) &&
                    id[1] == L':' &&
                    (id.size() == 2 || id[2] == L'\\')) {
                    isMassStorage = true;
                    break;
                }
            }

            if (fetched < 10) break;
        }

        if (isMassStorage) {
            keep[i] = false;
        }
    }

    // Output surviving entries
    fputs("[\n", stdout);
    bool first = true;
    for (size_t i = 0; i < entries.size(); i++) {
        if (!keep[i]) continue;
        if (!first) fputs(",\n", stdout);
        first = false;

        fputs("  {", stdout);
        WriteJsonString(stdout, "device_id", entries[i].deviceId, false);
        WriteJsonString(stdout, "friendly_name", entries[i].friendlyName, false);
        WriteJsonStringOrEmpty(stdout, "manufacturer", entries[i].manufacturer, true);
        fputs("}", stdout);
    }
    fputs("\n]\n", stdout);

    return 0;
}

// ---------------------------------------------------------------------------
// Open a device by PnP ID — returns IPortableDevice and IPortableDeviceContent
// ---------------------------------------------------------------------------
static HRESULT OpenDevice(
    PCWSTR deviceId,
    IPortableDevice** device,
    IPortableDeviceContent** content)
{
    *device = nullptr;
    *content = nullptr;

    ComPtr<IPortableDeviceValues> clientInfo;
    HRESULT hr = GetClientInformation(&clientInfo);
    if (FAILED(hr)) return hr;

    ComPtr<IPortableDevice> dev;
    hr = CoCreateInstance(CLSID_PortableDeviceFTM, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&dev));
    if (FAILED(hr)) return hr;

    hr = dev->Open(deviceId, clientInfo.Get());
    if (hr == E_ACCESSDENIED) {
        // Retry with read-only access (matching official sample fallback)
        clientInfo->SetUnsignedIntegerValue(WPD_CLIENT_DESIRED_ACCESS, GENERIC_READ);
        hr = dev->Open(deviceId, clientInfo.Get());
    }
    if (FAILED(hr)) return hr;

    ComPtr<IPortableDeviceContent> cnt;
    hr = dev->Content(&cnt);
    if (FAILED(hr)) return hr;

    *device = dev.Detach();
    *content = cnt.Detach();
    return S_OK;
}

// ---------------------------------------------------------------------------
// Trim trailing characters from a WPD string that are never valid in
// object names (control characters < 0x20, trailing spaces, padding bytes
// from fixed-width WPD driver buffers).
//
// Some WPD drivers (observed with Apple iPhone MTP implementations) return
// strings whose wcslen() extends past the actual name into padding/garbage
// from an internal fixed-size buffer.  This function trims those trailing
// non-filename characters while preserving legitimate name content.
// ---------------------------------------------------------------------------
static void TrimWpdName(std::wstring& s) {
    // Trim trailing characters that are not valid in any file system name:
    //   - Control characters (< 0x20)
    //   - DEL (0x7F)
    //   - Trailing spaces (common padding artifact)
    //   - Trailing null characters (not possible with proper wcslen, but
    //     be defensive against direct-pointer assignment)
    while (!s.empty()) {
        wchar_t c = s.back();
        if (c > 0 && c < 0x20) { s.pop_back(); continue; }
        if (c == 0x7F)          { s.pop_back(); continue; }
        if (c == L' ')          { s.pop_back(); continue; }
        if (c == L'\0')         { s.pop_back(); continue; }
        break;
    }
}

// ---------------------------------------------------------------------------
// Read a string property for a WPD object, trimming any trailing padding
// ---------------------------------------------------------------------------
static HRESULT GetObjectStringProperty(
    IPortableDeviceProperties* props,
    PCWSTR objectId,
    REFPROPERTYKEY key,
    std::wstring& out)
{
    ComPtr<IPortableDeviceKeyCollection> keys;
    HRESULT hr = CoCreateInstance(CLSID_PortableDeviceKeyCollection, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&keys));
    if (FAILED(hr)) return hr;

    hr = keys->Add(key);
    if (FAILED(hr)) return hr;

    ComPtr<IPortableDeviceValues> values;
    hr = props->GetValues(objectId, keys.Get(), &values);
    if (FAILED(hr)) return hr;

    PWSTR str = nullptr;
    hr = values->GetStringValue(key, &str);
    if (SUCCEEDED(hr) && str) {
        out.assign(str);
        TrimWpdName(out);
        CoTaskMemFree(str);
    } else {
        out.clear();
        if (str) CoTaskMemFree(str);
    }
    return hr;
}

// ---------------------------------------------------------------------------
// Read the best name for a WPD object: try ORIGINAL_FILE_NAME first, but
// also read NAME separately.  If ORIGINAL_FILE_NAME looks like it has
// trailing padding/garbage (after TrimWpdName it's empty or differs from
// NAME in a suspicious way) fall back to NAME.  This works around bugs in
// WPD drivers that return truncated/padded/garbage strings from
// ORIGINAL_FILE_NAME (common with Apple iPhone MTP).
// ---------------------------------------------------------------------------
static void GetObjectName(
    IPortableDeviceProperties* props,
    PCWSTR objectId,
    std::wstring& out)
{
    std::wstring origName, name;
    GetObjectStringProperty(props, objectId, WPD_OBJECT_ORIGINAL_FILE_NAME, origName);
    GetObjectStringProperty(props, objectId, WPD_OBJECT_NAME, name);

    // Trim both (TrimWpdName is already called inside GetObjectStringProperty,
    // but call it again explicitly in case properties were read earlier).
    TrimWpdName(origName);
    TrimWpdName(name);

    if (!origName.empty()) {
        // ORIGINAL_FILE_NAME is preferred when it's non-empty AND it either
        // matches NAME or NAME is empty (meaning the device genuinely doesn't
        // provide a separate display name).  If both are non-empty and they
        // differ, prefer NAME since it's less likely to have padding/garbage.
        if (name.empty() || origName == name) {
            out = origName;
        } else {
            // Both are non-empty and differ.  Use the SHORTER one as the
            // "real" name — padding always makes a string longer, never
            // shorter.
            out = (origName.size() <= name.size()) ? origName : name;
        }
    } else if (!name.empty()) {
        out = name;
    } else {
        out.clear();
    }
}

// ---------------------------------------------------------------------------
// Get a uint64 property for a WPD object
// ---------------------------------------------------------------------------
static HRESULT GetObjectUint64Property(
    IPortableDeviceProperties* props,
    PCWSTR objectId,
    REFPROPERTYKEY key,
    ULONGLONG& out)
{
    out = 0;
    ComPtr<IPortableDeviceKeyCollection> keys;
    HRESULT hr = CoCreateInstance(CLSID_PortableDeviceKeyCollection, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&keys));
    if (FAILED(hr)) return hr;

    hr = keys->Add(key);
    if (FAILED(hr)) return hr;

    ComPtr<IPortableDeviceValues> values;
    hr = props->GetValues(objectId, keys.Get(), &values);
    if (FAILED(hr)) return hr;

    return values->GetUnsignedLargeIntegerValue(key, &out);
}

// ---------------------------------------------------------------------------
// Get the content type GUID for an object and check if it's a folder
// ---------------------------------------------------------------------------
static bool IsFolder(IPortableDeviceProperties* props, PCWSTR objectId) {
    ComPtr<IPortableDeviceKeyCollection> keys;
    HRESULT hr = CoCreateInstance(CLSID_PortableDeviceKeyCollection, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&keys));
    if (FAILED(hr)) return false;
    keys->Add(WPD_OBJECT_CONTENT_TYPE);

    ComPtr<IPortableDeviceValues> values;
    hr = props->GetValues(objectId, keys.Get(), &values);
    if (FAILED(hr)) return false;

    GUID contentType = GUID_NULL;
    hr = values->GetGuidValue(WPD_OBJECT_CONTENT_TYPE, &contentType);
    if (FAILED(hr)) return false;
    if (IsEqualGUID(contentType, WPD_CONTENT_TYPE_FOLDER)) return true;
    if (IsEqualGUID(contentType, WPD_CONTENT_TYPE_FUNCTIONAL_OBJECT)) return true;
    return false;
}

// ---------------------------------------------------------------------------
// Resolve virtual path (e.g. "DCIM/100APPLE") to a WPD object ID.
// Walks the tree level by level, matching WPD_OBJECT_ORIGINAL_FILE_NAME
// or WPD_OBJECT_NAME at each level.
// ---------------------------------------------------------------------------
static HRESULT ResolvePath(
    IPortableDeviceContent* content,
    PCWSTR virtualPath,
    std::wstring& outObjectId)
{
    outObjectId.clear();

    ComPtr<IPortableDeviceProperties> props;
    HRESULT hr = content->Properties(&props);
    if (FAILED(hr)) return hr;

    // Start from the device root
    std::wstring currentParent = WPD_DEVICE_OBJECT_ID;

    // Parse the path into segments
    std::wstring path(virtualPath);
    std::vector<std::wstring> segments;
    size_t pos = 0;
    while (pos < path.size()) {
        size_t next = path.find(L'/', pos);
        if (next == std::wstring::npos) next = path.size();
        std::wstring seg = path.substr(pos, next - pos);
        if (!seg.empty()) {
            segments.push_back(seg);
        }
        pos = next + 1;
    }

    if (segments.empty() || (segments.size() == 1 && segments[0] == L".")) {
        // Empty path or "." means root
        outObjectId = WPD_DEVICE_OBJECT_ID;
        return S_OK;
    }

    // Walk each segment
    for (const auto& segment : segments) {
        // Enumerate children of currentParent
        ComPtr<IEnumPortableDeviceObjectIDs> enumObj;
        hr = content->EnumObjects(0, currentParent.c_str(), nullptr, &enumObj);
        if (FAILED(hr)) return hr;

        bool found = false;
        while (hr == S_OK) {
            DWORD numFetched = 0;
            PWSTR objectIds[10] = {};
            hr = enumObj->Next(10, objectIds, &numFetched);
            if (FAILED(hr)) break;

            for (DWORD i = 0; i < numFetched && objectIds[i]; i++) {
                std::wstring name;
                GetObjectName(props.Get(), objectIds[i], name);

                if (_wcsicmp(name.c_str(), segment.c_str()) == 0) {
                    currentParent = objectIds[i];
                    found = true;
                    break;
                }

                CoTaskMemFree(objectIds[i]);
                objectIds[i] = nullptr;
            }

            if (found) {
                // Free any remaining unfreed IDs from this batch
                for (DWORD i = 0; i < numFetched; i++) {
                    if (objectIds[i]) CoTaskMemFree(objectIds[i]);
                }
                break;
            }

            // Free freed ones already, free remaining
            for (DWORD i = 0; i < numFetched; i++) {
                if (objectIds[i]) CoTaskMemFree(objectIds[i]);
            }

            if (numFetched < 10) break; // No more children
        }

        if (!found) {
            // Transparent walk: at the device root, intermediate container
            // objects (e.g. "Internal Storage" on iPhones) often sit between
            // the root and the real filesystem tree.  If the segment wasn't
            // found among the root's direct children, look inside each
            // grandchild of the root to find it.
            if (currentParent == WPD_DEVICE_OBJECT_ID) {
                ComPtr<IEnumPortableDeviceObjectIDs> rootEnum;
                hr = content->EnumObjects(0, WPD_DEVICE_OBJECT_ID, nullptr, &rootEnum);
                if (SUCCEEDED(hr)) {
                    while (!found) {
                        DWORD fetched = 0;
                        PWSTR containerIds[10] = {};
                        hr = rootEnum->Next(10, containerIds, &fetched);
                        if (FAILED(hr)) break;

                        for (DWORD i = 0; i < fetched && containerIds[i]; i++) {
                            ComPtr<IEnumPortableDeviceObjectIDs> childEnum;
                            HRESULT childHr = content->EnumObjects(
                                0, containerIds[i], nullptr, &childEnum);
                            CoTaskMemFree(containerIds[i]);

                            if (FAILED(childHr)) continue;

                            while (childHr == S_OK && !found) {
                                DWORD childFetched = 0;
                                PWSTR childIds[10] = {};
                                childHr = childEnum->Next(
                                    10, childIds, &childFetched);
                                if (FAILED(childHr)) break;

                                for (DWORD j = 0; j < childFetched && childIds[j]; j++) {
                                    std::wstring childName;
                                    GetObjectName(props.Get(),
                                        childIds[j], childName);

                                    if (_wcsicmp(childName.c_str(),
                                            segment.c_str()) == 0) {
                                        currentParent = childIds[j];
                                        found = true;
                                        // Free remaining IDs in this batch
                                        for (DWORD k = j + 1;
                                             k < childFetched; k++) {
                                            if (childIds[k])
                                                CoTaskMemFree(childIds[k]);
                                        }
                                        break;
                                    }
                                    CoTaskMemFree(childIds[j]);
                                }
                            }
                        }

                        if (fetched < 10) break;
                    }
                }
            }

            if (!found) {
                return HRESULT_FROM_WIN32(ERROR_NOT_FOUND);
            }
        }
    }

    outObjectId = currentParent;
    return S_OK;
}

// ---------------------------------------------------------------------------
// list-folder subcommand
// ---------------------------------------------------------------------------
static int DoListFolder(PCWSTR deviceId, PCWSTR virtualPath) {
    ComPtr<IPortableDevice> device;
    ComPtr<IPortableDeviceContent> content;
    HRESULT hr = OpenDevice(deviceId, &device, &content);
    if (FAILED(hr)) {
        ReportError("device_open", L"Failed to open device", hr);
        return 1;
    }

    std::wstring folderId;
    hr = ResolvePath(content.Get(), virtualPath, folderId);
    if (FAILED(hr)) {
        ReportError("path_not_found", L"Path not found on device", hr);
        return 1;
    }

    ComPtr<IPortableDeviceProperties> props;
    hr = content->Properties(&props);
    if (FAILED(hr)) {
        ReportError("com_error", L"Failed to get IPortableDeviceProperties", hr);
        return 1;
    }

    // Enumerate children of the resolved folder
    ComPtr<IEnumPortableDeviceObjectIDs> enumObj;
    hr = content->EnumObjects(0, folderId.c_str(), nullptr, &enumObj);
    if (FAILED(hr)) {
        ReportError("com_error", L"Failed to enumerate folder contents", hr);
        return 1;
    }

    // Collect all children first so we can output valid JSON
    struct ChildInfo {
        std::wstring objectId;
        std::wstring name;
        bool isFolder;
        ULONGLONG size;
        std::wstring dateModified;
    };
    std::vector<ChildInfo> children;

    while (hr == S_OK) {
        DWORD numFetched = 0;
        PWSTR objectIds[10] = {};
        hr = enumObj->Next(10, objectIds, &numFetched);
        if (FAILED(hr)) break;

        for (DWORD i = 0; i < numFetched && objectIds[i]; i++) {
            ChildInfo ci;
            ci.objectId = objectIds[i];
            ci.isFolder = IsFolder(props.Get(), objectIds[i]);
            ci.size = 0;

            // Get name — prefer ORIGINAL_FILE_NAME, with WPD_OBJECT_NAME fallback
            GetObjectName(props.Get(), objectIds[i], ci.name);

            // Get size (null for folders)
            if (!ci.isFolder) {
                GetObjectUint64Property(props.Get(), objectIds[i],
                    WPD_OBJECT_SIZE, ci.size);
            }

            // Get date modified — try WPD_OBJECT_DATE_MODIFIED, then WPD_OBJECT_DATE_CREATED
            GetObjectStringProperty(props.Get(), objectIds[i],
                WPD_OBJECT_DATE_MODIFIED, ci.dateModified);
            if (ci.dateModified.empty()) {
                GetObjectStringProperty(props.Get(), objectIds[i],
                    WPD_OBJECT_DATE_CREATED, ci.dateModified);
            }

            children.push_back(std::move(ci));
            CoTaskMemFree(objectIds[i]);
            objectIds[i] = nullptr;
        }

        if (numFetched < 10) break;
    }

    // Output JSON
    fputs("[\n", stdout);
    for (size_t i = 0; i < children.size(); i++) {
        const auto& ci = children[i];
        fputs("  {", stdout);
        WriteJsonString(stdout, "object_id", ci.objectId, false);
        WriteJsonString(stdout, "name", ci.name, false);
        WriteJsonString(stdout, "type", ci.isFolder ? L"folder" : L"file", false);
        if (ci.isFolder) {
            WriteJsonNull(stdout, "size", false);
        } else {
            WriteJsonInt(stdout, "size", (long long)ci.size, false);
        }
        WriteJsonStringOrEmpty(stdout, "date_modified", ci.dateModified, true);
        fputs("}", stdout);
        if (i < children.size() - 1) fputc(',', stdout);
        fputc('\n', stdout);
    }
    fputs("]\n", stdout);

    return 0;
}

// ---------------------------------------------------------------------------
// debug-test subcommand — enumerates a folder and dumps raw string
// metadata (hex, wcslen, size) for every entry's ORIGINAL_FILE_NAME and
// NAME properties, so that any case where these don't agree or contain
// trailing garbage is immediately visible.
// ---------------------------------------------------------------------------
static int DoDebugTest(PCWSTR deviceId, PCWSTR virtualPath) {
    ComPtr<IPortableDevice> device;
    ComPtr<IPortableDeviceContent> content;
    HRESULT hr = OpenDevice(deviceId, &device, &content);
    if (FAILED(hr)) {
        ReportError("device_open", L"Failed to open device", hr);
        return 1;
    }

    std::wstring folderId;
    hr = ResolvePath(content.Get(), virtualPath, folderId);
    if (FAILED(hr)) {
        ReportError("path_not_found", L"Path not found on device", hr);
        return 1;
    }

    ComPtr<IPortableDeviceProperties> props;
    hr = content->Properties(&props);
    if (FAILED(hr)) {
        ReportError("com_error", L"Failed to get IPortableDeviceProperties", hr);
        return 1;
    }

    ComPtr<IEnumPortableDeviceObjectIDs> enumObj;
    hr = content->EnumObjects(0, folderId.c_str(), nullptr, &enumObj);
    if (FAILED(hr)) {
        ReportError("com_error", L"Failed to enumerate folder contents", hr);
        return 1;
    }

    struct DebugEntry {
        std::wstring objectId;
        std::wstring origFileName;
        std::wstring name;
        bool isFolder;
        ULONGLONG size;
        size_t origWcslen;
        size_t nameWcslen;
    };
    std::vector<DebugEntry> entries;

    while (hr == S_OK) {
        DWORD numFetched = 0;
        PWSTR objectIds[10] = {};
        hr = enumObj->Next(10, objectIds, &numFetched);
        if (FAILED(hr)) break;

        for (DWORD i = 0; i < numFetched && objectIds[i]; i++) {
            DebugEntry de;
            de.objectId = objectIds[i];
            de.isFolder = IsFolder(props.Get(), objectIds[i]);
            de.size = 0;

            // Read both properties using raw GetStringValue (no trimming)
            // so we can see exactly what the WPD driver returns.
            {
                ComPtr<IPortableDeviceKeyCollection> keys;
                if (SUCCEEDED(CoCreateInstance(CLSID_PortableDeviceKeyCollection, nullptr,
                        CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&keys)))) {
                    keys->Add(WPD_OBJECT_ORIGINAL_FILE_NAME);
                    keys->Add(WPD_OBJECT_NAME);
                    ComPtr<IPortableDeviceValues> vals;
                    if (SUCCEEDED(props->GetValues(objectIds[i], keys.Get(), &vals))) {
                        PWSTR p = nullptr;
                        if (SUCCEEDED(vals->GetStringValue(WPD_OBJECT_ORIGINAL_FILE_NAME, &p)) && p) {
                            de.origWcslen = wcslen(p);
                            de.origFileName.assign(p, de.origWcslen);
                            CoTaskMemFree(p);
                        } else {
                            de.origWcslen = 0;
                            if (p) CoTaskMemFree(p);
                        }
                        p = nullptr;
                        if (SUCCEEDED(vals->GetStringValue(WPD_OBJECT_NAME, &p)) && p) {
                            de.nameWcslen = wcslen(p);
                            de.name.assign(p, de.nameWcslen);
                            CoTaskMemFree(p);
                        } else {
                            de.nameWcslen = 0;
                            if (p) CoTaskMemFree(p);
                        }
                    }
                }
            }

            if (!de.isFolder) {
                GetObjectUint64Property(props.Get(), objectIds[i],
                    WPD_OBJECT_SIZE, de.size);
            }

            entries.push_back(std::move(de));
            CoTaskMemFree(objectIds[i]);
            objectIds[i] = nullptr;
        }

        if (numFetched < 10) break;
    }

    // Output JSON with full debug info
    fputs("[\n", stdout);
    for (size_t i = 0; i < entries.size(); i++) {
        const auto& e = entries[i];
        fputs("  {\n", stdout);
        fprintf(stdout, "    \"object_id\":\"%ws\",\n", e.objectId.c_str());

        // name with full metadata
        fprintf(stdout, "    \"name\":\"%ws\",\n", e.name.c_str());
        fprintf(stdout, "    \"name_wcslen\":%zu,\n", e.nameWcslen);
        fprintf(stdout, "    \"name_size\":%zu,\n", e.name.size());
        fputs("    \"name_hex\":\"", stdout);
        for (size_t j = 0; j < e.name.size(); j++) {
            fprintf(stdout, "%04x", (unsigned)e.name[j]);
        }
        fputs("\",\n", stdout);

        // original_file_name with full metadata
        fprintf(stdout, "    \"original_file_name\":\"%ws\",\n", e.origFileName.c_str());
        fprintf(stdout, "    \"orig_name_wcslen\":%zu,\n", e.origWcslen);
        fprintf(stdout, "    \"orig_name_size\":%zu,\n", e.origFileName.size());
        fputs("    \"orig_name_hex\":\"", stdout);
        for (size_t j = 0; j < e.origFileName.size(); j++) {
            fprintf(stdout, "%04x", (unsigned)e.origFileName[j]);
        }
        fputs("\",\n", stdout);

        // type and size
        fprintf(stdout, "    \"type\":\"%s\",\n", e.isFolder ? "folder" : "file");
        if (e.isFolder) {
            fputs("    \"size\":null\n", stdout);
        } else {
            fprintf(stdout, "    \"size\":%llu\n", (unsigned long long)e.size);
        }

        fputs("  }", stdout);
        if (i < entries.size() - 1) fputc(',', stdout);
        fputc('\n', stdout);
    }
    fputs("]\n", stdout);
    return 0;
}

// ---------------------------------------------------------------------------
// read-file subcommand
// ---------------------------------------------------------------------------
static int DoReadFile(PCWSTR deviceId, PCWSTR virtualPath) {
    // Switch stdout to binary mode BEFORE any output or COM calls that could
    // write to stdout. This prevents the C runtime from translating byte
    // sequences that look like line endings (\r\n, \n, \r, 0x1A) — which
    // would silently corrupt binary file data.
    int prevMode = _setmode(_fileno(stdout), _O_BINARY);

    ComPtr<IPortableDevice> device;
    ComPtr<IPortableDeviceContent> content;
    HRESULT hr = OpenDevice(deviceId, &device, &content);
    if (FAILED(hr)) {
        // Restore mode before writing error to stderr
        _setmode(_fileno(stdout), prevMode);
        ReportError("device_open", L"Failed to open device", hr);
        return 1;
    }

    std::wstring objectId;
    hr = ResolvePath(content.Get(), virtualPath, objectId);
    if (FAILED(hr)) {
        _setmode(_fileno(stdout), prevMode);
        ReportError("path_not_found", L"Path not found on device", hr);
        return 1;
    }

    // Get IPortableDeviceResources and obtain the default resource stream
    ComPtr<IPortableDeviceResources> resources;
    hr = content->Transfer(&resources);
    if (FAILED(hr)) {
        _setmode(_fileno(stdout), prevMode);
        ReportError("com_error", L"Failed to get IPortableDeviceResources", hr);
        return 1;
    }

    DWORD optimalTransferSize = 0;
    ComPtr<IStream> dataStream;
    hr = resources->GetStream(objectId.c_str(),
                              WPD_RESOURCE_DEFAULT,
                              STGM_READ,
                              &optimalTransferSize,
                              &dataStream);
    if (FAILED(hr)) {
        _setmode(_fileno(stdout), prevMode);
        ReportError("stream_error", L"Failed to get data stream for object", hr);
        return 1;
    }

    // Use the optimal transfer size if the driver provides one, otherwise
    // default to 256 KB. The driver's suggestion is ideal because it's
    // tuned to the device's USB transfer characteristics. If the driver
    // doesn't report one (rare but possible), 256 KB is a reasonable
    // middle ground: large enough to avoid excessive Read() system call
    // overhead, small enough to avoid memory pressure, and well within
    // typical USB transfer buffer sizes.
    DWORD bufferSize = (optimalTransferSize > 0) ? optimalTransferSize : (256 * 1024);

    std::vector<BYTE> buffer(bufferSize);
    ULONG bytesRead = 0;
    size_t totalWritten = 0;

    do {
        hr = dataStream->Read(buffer.data(), bufferSize, &bytesRead);
        if (FAILED(hr)) {
            _setmode(_fileno(stdout), prevMode);
            ReportError("stream_error", L"Error reading data stream", hr);
            return 1;
        }

        if (bytesRead > 0) {
            size_t written = fwrite(buffer.data(), 1, bytesRead, stdout);
            if (written != bytesRead) {
                _setmode(_fileno(stdout), prevMode);
                ReportError("write_error", L"Failed to write all bytes to stdout", E_FAIL);
                return 1;
            }
            totalWritten += written;
        }
    } while (bytesRead > 0);

    // Restore stdout mode
    _setmode(_fileno(stdout), prevMode);

    return 0;
}

// ---------------------------------------------------------------------------
// Argument parsing
// ---------------------------------------------------------------------------
struct Args {
    std::wstring command;
    std::wstring deviceId;
    std::wstring path;
};

static int ParseArgs(int argc, wchar_t* argv[], Args& args) {
    if (argc < 2) return -1;

    args.command = argv[1];

    for (int i = 2; i < argc; i++) {
        std::wstring arg = argv[i];
        if ((arg == L"--device" || arg == L"-d") && i + 1 < argc) {
            args.deviceId = argv[++i];
        } else if ((arg == L"--path" || arg == L"-p") && i + 1 < argc) {
            args.path = argv[++i];
        } else {
            return -1;
        }
    }
    return 0;
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------
int wmain(int argc, wchar_t* argv[]) {
    // Enable heap corruption termination (matching official sample)
    HeapSetInformation(nullptr, HeapEnableTerminationOnCorruption, nullptr, 0);

    ComInitGuard comGuard;
    if (FAILED(comGuard.hr())) {
        ReportError("com_error", L"Failed to initialize COM", comGuard.hr());
        return 1;
    }

    Args args;
    if (ParseArgs(argc, argv, args) != 0) {
        fputs("Usage:\n", stderr);
        fputs("  wpd_helper.exe list-devices\n", stderr);
        fputs("  wpd_helper.exe debug-test --device <device_id> --path <virtual_path>\n", stderr);
        fputs("  wpd_helper.exe list-folder --device <device_id> --path <virtual_path>\n", stderr);
        fputs("  wpd_helper.exe read-file --device <device_id> --path <virtual_path>\n", stderr);
        return 1;
    }

    if (args.command == L"list-devices") {
        return DoListDevices();
    } else if (args.command == L"debug-test") {
        if (args.deviceId.empty() || args.path.empty()) {
            ReportError("invalid_args", L"--device and --path are required for debug-test");
            return 1;
        }
        return DoDebugTest(args.deviceId.c_str(), args.path.c_str());
    } else if (args.command == L"list-folder") {
        if (args.deviceId.empty() || args.path.empty()) {
            ReportError("invalid_args", L"--device and --path are required for list-folder");
            return 1;
        }
        return DoListFolder(args.deviceId.c_str(), args.path.c_str());
    } else if (args.command == L"read-file") {
        if (args.deviceId.empty() || args.path.empty()) {
            ReportError("invalid_args", L"--device and --path are required for read-file");
            return 1;
        }
        return DoReadFile(args.deviceId.c_str(), args.path.c_str());
    } else {
        ReportError("invalid_command", L"Unknown command: " + args.command);
        return 1;
    }
}

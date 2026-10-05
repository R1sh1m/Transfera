import type { IOSDeviceInfo } from "@/types/api";

export type DeviceCategory =
  | "iphone"
  | "ipad"
  | "ipod"
  | "android_phone"
  | "android_tablet"
  | "camera"
  | "removable_drive"
  | "generic_device";

export interface DeviceMeta {
  category: DeviceCategory;
  displayName: string;
  categoryLabel: string;
  shortName: string;
  brand: string;
  platformBadge: string;
  iconType: "smartphone" | "tablet" | "camera" | "hard-drive" | "usb";
  accentColor: {
    bg: string;
    text: string;
    border: string;
    badgeBg: string;
    badgeText: string;
  };
}

/**
 * Identify device brand, category, icon, and formatted name
 * across iPhones, iPads, Android smartphones, Android tablets,
 * digital cameras, and USB/SD removable storage.
 */
export function getDeviceMeta(
  device?: {
    name?: string;
    model?: string;
    serial?: string;
    ios_version?: string;
    connection_type?: string;
  } | null,
): DeviceMeta {
  const name = (device?.name || "").trim();
  const model = (device?.model || "").trim();
  const serial = (device?.serial || "").trim();
  const version = (device?.ios_version || "").trim().toLowerCase();

  const nameLower = name.toLowerCase();
  const modelLower = model.toLowerCase();
  const serialLower = serial.toLowerCase();

  // 1. Removable drives (e.g. E:\, D:\ or drive letters)
  if (
    /^[a-zA-Z]:[/\\]?/.test(serial) ||
    /^[a-zA-Z]:[/\\]?/.test(name) ||
    nameLower.includes("sd card") ||
    nameLower.includes("usb drive") ||
    nameLower.includes("flash drive")
  ) {
    return {
      category: "removable_drive",
      categoryLabel: "Removable Storage",
      displayName: name || model || "Removable Drive",
      shortName: name || "SD/USB",
      brand: "Removable",
      platformBadge: "SD / USB",
      iconType: "hard-drive",
      accentColor: {
        bg: "bg-amber-50 dark:bg-amber-950/40",
        text: "text-amber-600 dark:text-amber-400",
        border: "border-amber-300 dark:border-amber-800",
        badgeBg: "bg-amber-100 dark:bg-amber-900/40",
        badgeText: "text-amber-700 dark:text-amber-300",
      },
    };
  }

  // 2. Apple iPad
  if (
    modelLower.includes("ipad") ||
    nameLower.includes("ipad")
  ) {
    const cleanName = name || "Apple iPad";
    return {
      category: "ipad",
      categoryLabel: "Apple iPad",
      displayName: cleanName.toLowerCase().startsWith("apple") ? cleanName : `Apple ${cleanName}`,
      shortName: model || "iPad",
      brand: "Apple",
      platformBadge: "iPadOS",
      iconType: "tablet",
      accentColor: {
        bg: "bg-indigo-50 dark:bg-indigo-950/40",
        text: "text-indigo-600 dark:text-indigo-400",
        border: "border-indigo-300 dark:border-indigo-800",
        badgeBg: "bg-indigo-100 dark:bg-indigo-900/40",
        badgeText: "text-indigo-700 dark:text-indigo-300",
      },
    };
  }

  // 3. Apple iPod
  if (modelLower.includes("ipod") || nameLower.includes("ipod")) {
    return {
      category: "ipod",
      categoryLabel: "Apple iPod",
      displayName: name || "Apple iPod",
      shortName: model || "iPod",
      brand: "Apple",
      platformBadge: "iOS",
      iconType: "smartphone",
      accentColor: {
        bg: "bg-blue-50 dark:bg-blue-950/40",
        text: "text-blue-600 dark:text-blue-400",
        border: "border-blue-300 dark:border-blue-800",
        badgeBg: "bg-blue-100 dark:bg-blue-900/40",
        badgeText: "text-blue-700 dark:text-blue-300",
      },
    };
  }

  // 4. Digital Cameras (Canon, Nikon, Sony Alpha, Lumix, Fujifilm, GoPro, etc.)
  const isCamera =
    nameLower.includes("canon") ||
    modelLower.includes("canon") ||
    nameLower.includes("nikon") ||
    modelLower.includes("nikon") ||
    nameLower.includes("sony dsc") ||
    nameLower.includes("alpha ") ||
    modelLower.includes("alpha ") ||
    nameLower.includes("lumix") ||
    modelLower.includes("lumix") ||
    nameLower.includes("fujifilm") ||
    modelLower.includes("fujifilm") ||
    nameLower.includes("gopro") ||
    modelLower.includes("gopro") ||
    nameLower.includes("powershot") ||
    nameLower.includes("coolpix");

  if (isCamera) {
    let brand = "Camera";
    if (nameLower.includes("canon") || modelLower.includes("canon")) brand = "Canon";
    else if (nameLower.includes("nikon") || modelLower.includes("nikon")) brand = "Nikon";
    else if (nameLower.includes("sony") || modelLower.includes("sony")) brand = "Sony";
    else if (nameLower.includes("fuji")) brand = "Fujifilm";
    else if (nameLower.includes("gopro")) brand = "GoPro";
    else if (nameLower.includes("lumix")) brand = "Panasonic";

    return {
      category: "camera",
      categoryLabel: "Digital Camera",
      displayName: name || model || "Digital Camera",
      shortName: model || brand,
      brand,
      platformBadge: "Camera",
      iconType: "camera",
      accentColor: {
        bg: "bg-amber-50 dark:bg-amber-950/40",
        text: "text-amber-600 dark:text-amber-400",
        border: "border-amber-300 dark:border-amber-800",
        badgeBg: "bg-amber-100 dark:bg-amber-900/40",
        badgeText: "text-amber-700 dark:text-amber-300",
      },
    };
  }

  // 5. Apple iPhone detection
  const isExplicitApple =
    modelLower.includes("iphone") ||
    nameLower.includes("iphone") ||
    modelLower.includes("apple") ||
    nameLower.includes("apple") ||
    serialLower.includes("vid_05ac") ||
    (version !== "" && version !== "unknown" && !version.includes("android"));

  if (isExplicitApple) {
    const cleanName = name || "Apple iPhone";
    const displayName = cleanName.toLowerCase().startsWith("apple")
      ? cleanName
      : `Apple ${cleanName}`;
    return {
      category: "iphone",
      categoryLabel: "Apple iPhone",
      displayName,
      shortName: model || "iPhone",
      brand: "Apple",
      platformBadge: "iOS",
      iconType: "smartphone",
      accentColor: {
        bg: "bg-blue-50 dark:bg-blue-950/40",
        text: "text-blue-600 dark:text-blue-400",
        border: "border-blue-300 dark:border-blue-800",
        badgeBg: "bg-blue-100 dark:bg-blue-900/40",
        badgeText: "text-blue-700 dark:text-blue-300",
      },
    };
  }

  // 6. Android Tablets
  const isAndroidTablet =
    (nameLower.includes("tab") ||
      modelLower.includes("tab") ||
      nameLower.includes("pad") ||
      modelLower.includes("pad")) &&
    (nameLower.includes("galaxy") ||
      modelLower.includes("samsung") ||
      nameLower.includes("lenovo") ||
      modelLower.includes("lenovo") ||
      nameLower.includes("xiaomi") ||
      modelLower.includes("xiaomi"));

  if (isAndroidTablet) {
    let brand = "Android";
    if (modelLower.includes("samsung") || nameLower.includes("galaxy")) brand = "Samsung";
    else if (modelLower.includes("lenovo") || nameLower.includes("lenovo")) brand = "Lenovo";
    else if (modelLower.includes("xiaomi")) brand = "Xiaomi";

    const displayName = name.toLowerCase().includes(brand.toLowerCase())
      ? name
      : `${brand} ${name || model || "Tablet"}`;

    return {
      category: "android_tablet",
      categoryLabel: "Android Tablet",
      displayName: displayName.trim(),
      shortName: model || "Android Tablet",
      brand,
      platformBadge: "Android",
      iconType: "tablet",
      accentColor: {
        bg: "bg-emerald-50 dark:bg-emerald-950/40",
        text: "text-emerald-600 dark:text-emerald-400",
        border: "border-emerald-300 dark:border-emerald-800",
        badgeBg: "bg-emerald-100 dark:bg-emerald-900/40",
        badgeText: "text-emerald-700 dark:text-emerald-300",
      },
    };
  }

  // 7. Android Smartphones (Samsung, Google Pixel, OnePlus, Xiaomi, Motorola, Oppo, Vivo, Sony, etc.)
  let androidBrand = "Android";
  if (
    modelLower.includes("samsung") ||
    nameLower.includes("galaxy") ||
    serialLower.includes("vid_04e8")
  ) {
    androidBrand = "Samsung";
  } else if (
    modelLower.includes("google") ||
    nameLower.includes("pixel") ||
    modelLower.includes("pixel") ||
    serialLower.includes("vid_18d1")
  ) {
    androidBrand = "Google";
  } else if (
    modelLower.includes("oneplus") ||
    nameLower.includes("oneplus") ||
    serialLower.includes("vid_2a70")
  ) {
    androidBrand = "OnePlus";
  } else if (
    modelLower.includes("xiaomi") ||
    nameLower.includes("xiaomi") ||
    nameLower.includes("redmi") ||
    modelLower.includes("redmi") ||
    nameLower.includes("poco") ||
    serialLower.includes("vid_2717")
  ) {
    androidBrand = "Xiaomi";
  } else if (
    modelLower.includes("motorola") ||
    nameLower.includes("moto") ||
    serialLower.includes("vid_22b8")
  ) {
    androidBrand = "Motorola";
  } else if (
    modelLower.includes("sony") ||
    nameLower.includes("xperia") ||
    serialLower.includes("vid_0fce")
  ) {
    androidBrand = "Sony";
  } else if (
    modelLower.includes("oppo") ||
    nameLower.includes("oppo") ||
    modelLower.includes("vivo") ||
    nameLower.includes("vivo") ||
    modelLower.includes("realme") ||
    nameLower.includes("realme") ||
    modelLower.includes("nothing") ||
    nameLower.includes("nothing") ||
    modelLower.includes("huawei") ||
    nameLower.includes("huawei") ||
    modelLower.includes("honor") ||
    nameLower.includes("honor")
  ) {
    androidBrand = "Android";
  }

  const rawName = name || model || "Android Device";
  const displayName =
    androidBrand !== "Android" && !rawName.toLowerCase().includes(androidBrand.toLowerCase())
      ? `${androidBrand} ${rawName}`
      : rawName;

  return {
    category: "android_phone",
    categoryLabel: "Android Smartphone",
    displayName: displayName.trim(),
    shortName: model || "Android",
    brand: androidBrand,
    platformBadge: "Android",
    iconType: "smartphone",
    accentColor: {
      bg: "bg-emerald-50 dark:bg-emerald-950/40",
      text: "text-emerald-600 dark:text-emerald-400",
      border: "border-emerald-300 dark:border-emerald-800",
      badgeBg: "bg-emerald-100 dark:bg-emerald-900/40",
      badgeText: "text-emerald-700 dark:text-emerald-300",
    },
  };
}

/**
 * Format a device URI into a clean, human-readable string using the
 * list of currently discovered devices.
 */
export function formatDevicePath(
  path: string | null | undefined,
  devices?: IOSDeviceInfo[],
): {
  isDevice: boolean;
  cleanPath: string;
  meta: DeviceMeta;
  internalPath: string;
} {
  if (!path) {
    return {
      isDevice: false,
      cleanPath: "",
      meta: getDeviceMeta(null),
      internalPath: "",
    };
  }

  const isIos = path.startsWith("ios://");
  const isWpd = path.startsWith("wpd://");

  if (!isIos && !isWpd) {
    return {
      isDevice: false,
      cleanPath: path,
      meta: getDeviceMeta({ name: path, model: "Local Folder" }),
      internalPath: path,
    };
  }

  const rawWithoutPrefix = isIos
    ? path.slice("ios://".length)
    : path.slice("wpd://".length);
  const slashIdx = rawWithoutPrefix.indexOf("/");
  const serial = slashIdx === -1 ? rawWithoutPrefix : rawWithoutPrefix.slice(0, slashIdx);
  const subPath = slashIdx === -1 ? "/DCIM" : rawWithoutPrefix.slice(slashIdx);

  // Look up matching device in discovered devices list
  const matchedDevice = devices?.find(
    (d) =>
      d.serial.toLowerCase() === serial.toLowerCase() ||
      (d.serial.startsWith("\\\\?\\") && serial.startsWith("\\\\?\\")),
  );

  const meta = getDeviceMeta(
    matchedDevice || {
      serial,
      name: isIos ? "Apple Device" : "MTP Device",
      model: isIos ? "iOS Device" : "Android / MTP",
    },
  );

  const cleanSubPath = subPath === "/" || subPath === "/DCIM" ? "DCIM" : subPath.replace(/^\//, "");
  const cleanPath = `${meta.displayName} (${cleanSubPath})`;

  return {
    isDevice: true,
    cleanPath,
    meta,
    internalPath: subPath,
  };
}

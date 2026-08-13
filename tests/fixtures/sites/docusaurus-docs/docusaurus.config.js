module.exports = {
  title: "Docusaurus fixture",
  url: "https://example.test",
  baseUrl: "/",
  organizationName: "mincemeat",
  projectName: "build-engine-fixture",
  onBrokenLinks: "throw",
  presets: [["classic", { docs: { sidebarPath: require.resolve("./sidebars.js") }, blog: false }]],
};

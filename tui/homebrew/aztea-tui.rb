class AzteaTui < Formula
  include Language::Python::Virtualenv

  desc "Terminal UI for the Aztea AI agent marketplace"
  homepage "https://aztea.ai"
  url "https://files.pythonhosted.org/packages/source/a/aztea-tui/aztea-tui-0.1.0.tar.gz"
  # Update sha256 after publishing to PyPI:
  # sha256 "REPLACE_WITH_ACTUAL_SHA256"
  license "MIT"

  depends_on "python@3.11"

  resource "textual" do
    url "https://files.pythonhosted.org/packages/source/t/textual/textual-0.89.1.tar.gz"
    # sha256 "REPLACE"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/source/r/rich/rich-13.9.4.tar.gz"
    # sha256 "REPLACE"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "0.1.0", shell_output("#{bin}/aztea-tui --version 2>&1", 0)
  end
end

import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

df = pd.read_csv("All_Diets.csv")

# Safe fillna for numeric columns only
df.fillna(df.select_dtypes(include='number').mean(), inplace=True)

# Average macros per diet type
avg_macros = df.groupby('Diet_type')[['Protein(g)', 'Carbs(g)', 'Fat(g)']].mean().reset_index()

# Top 5 protein-rich recipes per diet type
top_protein = df.sort_values('Protein(g)', ascending=False).groupby('Diet_type').head(5)

# Ratios
df['Protein_to_Carbs_ratio'] = df['Protein(g)'] / df['Carbs(g)']
df['Carbs_to_Fat_ratio'] = df['Carbs(g)'] / df['Fat(g)']

# Plot
plt.figure(figsize=(10, 6))
sns.barplot(x='Diet_type', y='Protein(g)', data=avg_macros)
plt.title('Average Protein by Diet Type')
plt.ylabel('Average Protein (g)')
plt.xlabel('Diet Type')
plt.xticks(rotation=45)
plt.tight_layout()
plt.show()